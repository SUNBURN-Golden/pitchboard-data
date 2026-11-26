"""Queue health between the daemon and the daily collector (2026-11-26): queued copies of the same work collapse,
refresh gets a fixed share of leases, the refresh step leases only freshness jobs, budgets and the daily-window stop
use wall-clock time, and lock holders are waited for instead of failing the run."""
import fcntl
import os
import shutil
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pbdata import db
from pbdata.collect import collector, scheduler
from pbdata.collect.wiki import Api
from pbdata.daemon import cli as daemon_cli

NOW = "2026-11-26T01:00:00Z"


class FakeFetcher:
    requests = 0


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        for d in ("service_db", "infra_db", "locks"):
            os.makedirs(os.path.join(self.root, d))
        self.svc = db.open_service(os.path.join(self.root, "service_db", "s.sqlite3"))
        self.infra = db.open_infra(os.path.join(self.root, "infra_db", "i.sqlite3"))
        self.handled = []

        def h(ctx, payload, job):
            self.handled.append((job["budget_class"], payload.get("url") or payload.get("i")))
            return {"state": "ok"}
        p = patch.dict(collector.HANDLERS, {"fetch_page": h, "discover": h})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.svc.close()
        self.infra.close()
        shutil.rmtree(self.root)

    def ctx(self, api=None):
        return collector.Ctx(self.svc, self.infra, FakeFetcher(), api or Api(sleep=lambda s: None), {"targets": []}, "run_t", "t",
                             lambda: NOW)

    def state(self, jid):
        return self.infra.execute("SELECT state FROM job WHERE job_id=?", (jid,)).fetchone()["state"]


class Collapse(Base):
    def test_copies_from_several_runs_collapse_to_one(self):
        ids = [scheduler.enqueue(self.infra, "fetch_page", "freshness", {"url": "https://a.example/x", "via": "refresh"},
                                 "fetch|https://a.example/x|tag%d" % i, 0.2 + i / 100, NOW) for i in range(3)]
        other = scheduler.enqueue(self.infra, "fetch_page", "freshness", {"url": "https://a.example/y", "via": "refresh"}, "fetch|y|t0", 0.2, NOW)
        self.assertEqual(2, scheduler.collapse_duplicates(self.infra, NOW))
        self.assertEqual(["done", "done", "pending"], [self.state(j) for j in ids])   # highest priority copy kept
        self.assertEqual("pending", self.state(other))

    def test_leased_copy_is_kept_and_never_touched(self):
        a = scheduler.enqueue(self.infra, "discover", "discovery", {"target_id": "t", "strategy": "s"}, "discover|t|s|r1", 0.3, NOW)
        b = scheduler.enqueue(self.infra, "discover", "discovery", {"target_id": "t", "strategy": "s"}, "discover|t|s|r2", 0.9, NOW)
        self.infra.execute("UPDATE job SET state='leased' WHERE job_id=?", (a,))
        self.assertEqual(1, scheduler.collapse_duplicates(self.infra, NOW))
        self.assertEqual(("leased", "done"), (self.state(a), self.state(b)))

    def test_refresh_enqueue_skips_queued_url_and_carries_higher_priority(self):
        jid = scheduler.enqueue(self.infra, "fetch_page", "freshness", {"url": "https://a.example/x", "via": "refresh", "site_source_id": "s", "depth": 0},
                                "fetch|https://a.example/x|old", 0.2, NOW)
        cands = [{"url": "https://a.example/x", "source_id": "src", "depth": 0, "refresh_fresh_until": NOW}]
        with patch.object(collector, "refresh_candidates", return_value=cands):
            ctx = self.ctx()
            ctx.svc = type("Svc", (), {"execute": staticmethod(self._owner_exec(self.svc.execute))})()
            self.assertEqual(0, collector.enqueue_refresh(ctx))
        self.assertEqual(scheduler.LIVE_CALL_REFRESH_PRIORITY,
                         self.infra.execute("SELECT priority FROM job WHERE job_id=?", (jid,)).fetchone()[0])
        self.assertEqual(1, self.infra.execute("SELECT count(*) FROM job").fetchone()[0])

    @staticmethod
    def _owner_exec(real):
        class Row(dict):
            pass

        class Cur:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        def ex(sql, args=()):
            if "FROM pb_source s JOIN pb_source_site" in sql:
                return Cur(Row(source_id="site"))
            if "min(json_extract" in sql:
                return Cur(Row(d=None))
            return real(sql, args)
        return ex


class Fairness(Base):
    def fill(self):
        for i in range(4):
            scheduler.enqueue(self.infra, "discover", "discovery", {"target_id": "t%d" % i, "strategy": "s", "i": i}, "d%d" % i, 0.9, NOW)
            scheduler.enqueue(self.infra, "fetch_page", "freshness", {"url": "https://a.example/%d" % i, "via": "refresh"}, "f%d" % i, 0.2, NOW)

    def test_freshness_gets_every_other_lease(self):
        self.fill()
        collector.run(self.ctx(), max_jobs=4, log=None)
        self.assertEqual(["freshness", "discovery", "freshness", "discovery"], [c for c, _ in self.handled])

    def test_pure_priority_when_disabled(self):
        self.fill()
        collector.run(self.ctx(), max_jobs=4, log=None, freshness_every=0)
        self.assertEqual(["discovery"] * 4, [c for c, _ in self.handled])

    def test_refresh_lane_only(self):
        self.fill()
        collector.run(self.ctx(), log=None, lanes=("freshness",))
        self.assertEqual(["freshness"] * 4, [c for c, _ in self.handled])
        self.assertEqual(4, self.infra.execute("SELECT count(*) FROM job WHERE state='pending' AND budget_class='discovery'").fetchone()[0])


class WallClock(Base):
    def test_stop_at_in_the_past_leases_nothing(self):
        scheduler.enqueue(self.infra, "discover", "discovery", {"target_id": "t", "strategy": "s"}, "d", 0.9, NOW)
        stats = collector.run(self.ctx(), log=None, stop_at=time.time() - 1)
        self.assertEqual(0, stats["jobs_done"])
        self.assertEqual(1, stats["stopped_time_budget"])

    def test_default_budget_clock_is_wall_time(self):
        import inspect
        self.assertIs(time.time, inspect.signature(collector.run).parameters["monotonic"].default)

    def test_api_stops_calling_after_deadline_and_is_restored(self):
        api = Api(sleep=lambda s: None)
        api.deadline = time.time() - 1
        with patch("pbdata.collect.egress.urlopen") as op:
            self.assertIsNone(api.get("https://www.wikidata.org/w/api.php", {}))
            op.assert_not_called()
        api.deadline = None
        ctx = self.ctx(api)
        collector.run(ctx, log=None, time_budget_s=100)
        self.assertIsNone(api.deadline)

    def test_discover_job_api_slice(self):
        seen = []

        def disc(ctx, payload, job):
            seen.append(ctx.api.deadline)
            return {"state": "ok"}
        scheduler.enqueue(self.infra, "discover", "discovery", {"target_id": "t", "strategy": "s"}, "d", 0.9, NOW)
        api = Api(sleep=lambda s: None)
        clock = [1000.0]
        with patch.dict(collector.HANDLERS, {"discover": disc}), patch.dict(os.environ, {"PB_DISCOVER_JOB_MAX_S": "600"}):
            collector.run(self.ctx(api), log=None, time_budget_s=7200, monotonic=lambda: clock[0])
        self.assertEqual([1600.0], seen)          # min(job slice, run deadline 8200)
        self.assertIsNone(api.deadline)

    def test_cli_lanes_flag(self):
        from pbdata.collect import cli
        seen = {}
        real = collector.run

        def spy(ctx, *a, **k):
            seen.update(k)
            return real(ctx, *a, **k)
        with patch.object(collector, "run", spy), patch("sys.stdout"):
            cli.main(["run", "--root", self.root, "--no-seed", "--lanes", "freshness,backfill", "--max-jobs", "0"])
        self.assertEqual(("freshness", "backfill"), seen["lanes"])

    def test_next_window_stop(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()
        try:
            def at(h, m):
                return time.mktime((2026, 11, 26, h, m, 0, 0, 0, -1))
            self.assertEqual(at(3, 50), daemon_cli.next_window_stop(at(1, 0), "04:20", 1800))
            self.assertEqual(at(3, 50), daemon_cli.next_window_stop(at(4, 0), "04:20", 1800))      # inside margin: already past
            self.assertEqual(at(3, 50) + 86400, daemon_cli.next_window_stop(at(7, 36), "04:20", 1800))
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()


class Locks(Base):
    def test_runlock_waits_for_holder(self):
        p = os.path.join(self.root, "locks", "collector.lock")
        fh = open(p, "a")
        fcntl.flock(fh, fcntl.LOCK_EX)
        with self.assertRaises(RuntimeError):
            with scheduler.RunLock(p):
                pass
        threading.Timer(0.3, lambda: (fcntl.flock(fh, fcntl.LOCK_UN), fh.close())).start()
        with scheduler.RunLock(p, wait_s=10):
            pass

    def test_daemon_does_not_start_while_daily_runs(self):
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        fh = open(os.path.join(self.root, "locks", "daily.lock"), "a")
        fcntl.flock(fh, fcntl.LOCK_EX)
        self.addCleanup(fh.close)
        with patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(3, daemon_cli.main(["run", "--root", self.root, "--offline", "--lock-wait", "0"]))

    def test_daemon_sets_window_stop(self):
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        seen = {}

        class Runner:
            def __init__(self, ctx, stages):
                seen.update(ctx.opts)

            def run(self):
                return {"stages": {}}
        with patch.object(daemon_cli, "StageRunner", Runner), patch("sys.stdout"):
            daemon_cli.main(["run", "--root", self.root, "--offline"])
        self.assertGreater(seen["stop_at"], time.time())
        self.assertLessEqual(seen["stop_at"], time.time() + 86400)


if __name__ == "__main__":
    unittest.main()
