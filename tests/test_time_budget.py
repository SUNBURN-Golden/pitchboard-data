"""Wall-clock budgets: the daemon's fetch stage and the collector loop stop leasing jobs when the budget is spent,
leave the rest queued, and release their locks; handoff never holds part of the pipeline lock group while waiting."""
import fcntl
import os
import shutil
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pbdata import db
from pbdata.collect import collector, scheduler
from pbdata.collect.wiki import Api
from pbdata.daemon import cli as daemon_cli
from pbdata.daemon import stages as S
from pbdata.daemon.runner import DaemonLock, Ledger, RunContext
from pbdata.handoff import status

NOW = "2026-11-24T01:00:00Z"


class FakeFetcher:
    requests = 0


class Clock:
    """Fake monotonic clock: every handled job costs 600 s."""
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        for d in ("service_db", "infra_db", "locks"):
            os.makedirs(os.path.join(self.root, d))
        self.svc = db.open_service(os.path.join(self.root, "service_db", "s.sqlite3"))
        self.infra = db.open_infra(os.path.join(self.root, "infra_db", "i.sqlite3"))
        self.clock = Clock()
        self.handled = []
        for i in range(10):
            scheduler.enqueue(self.infra, "slow_test", "discovery", {"i": i}, "slow|%d" % i, 1.0, NOW)
        self.infra.commit()

        def slow(ctx, payload, job):
            self.handled.append(payload["i"])
            self.clock.t += 600
            return {"state": "ok"}
        p = patch.dict(collector.HANDLERS, {"slow_test": slow})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.svc.close()
        self.infra.close()
        shutil.rmtree(self.root)

    def ctx(self):
        return collector.Ctx(self.svc, self.infra, FakeFetcher(), Api(sleep=lambda s: None), {"targets": []}, "run_t", "t", lambda: NOW)

    def pending(self):
        return self.infra.execute("SELECT count(*) FROM job WHERE state IN ('pending','retry_wait')").fetchone()[0]


class CollectorBudget(Base):
    def test_budget_stops_leasing_and_keeps_rest_queued(self):
        stats = collector.run(self.ctx(), log=None, time_budget_s=1800, monotonic=self.clock)
        self.assertEqual(3, len(self.handled))
        self.assertEqual(1, stats["stopped_time_budget"])
        self.assertEqual(7, self.pending())
        self.assertEqual(0, self.infra.execute("SELECT count(*) FROM job WHERE state='leased'").fetchone()[0])

    def test_no_budget_runs_whole_queue(self):
        stats = collector.run(self.ctx(), log=None, monotonic=self.clock)
        self.assertEqual(10, len(self.handled))
        self.assertNotIn("stopped_time_budget", stats)

    def test_next_run_resumes_queue(self):
        collector.run(self.ctx(), log=None, time_budget_s=1800, monotonic=self.clock)
        collector.run(self.ctx(), log=None, time_budget_s=1800, monotonic=self.clock)
        self.assertEqual(list(range(6)), sorted(self.handled))


class DaemonFetchStageBudget(Base):
    def test_stage_respects_budget_and_releases_locks(self):
        lock_dir = os.path.join(self.root, "locks")
        opts = {"targets": {"targets": []}, "fetch_budget_s": 1200, "monotonic": self.clock, "quiet": True,
                "fetcher": FakeFetcher(), "api": Api(sleep=lambda s: None), "refresh_limit": 0}
        c = RunContext(self.svc, self.infra, self.root, "dmn_t", lambda: NOW, Ledger(self.infra, NOW[:10]), opts)
        with DaemonLock(os.path.join(lock_dir, "daemon.lock")), DaemonLock(os.path.join(lock_dir, "collector.lock")):
            out = S.s_incremental_fetch(c)
        self.assertTrue(out["stopped_time_budget"])
        self.assertEqual(2, out["jobs_done"])
        self.assertEqual(8, out["jobs_pending_after"])
        for name in ("daemon.lock", "collector.lock"):      # the daily collector can take them right away
            with open(os.path.join(lock_dir, name), "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_cli_default_budget_and_env_override(self):
        self.assertEqual(90 * 60, daemon_cli.DEFAULT_FETCH_BUDGET_S)
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))
        seen = {}

        class Runner:
            def __init__(self, ctx, stages):
                seen.update(ctx.opts)

            def run(self):
                return {"stages": {}}
        with patch.object(daemon_cli, "StageRunner", Runner), patch("sys.stdout"):
            daemon_cli.main(["run", "--root", self.root, "--offline"])
            self.assertEqual(5400, seen["fetch_budget_s"])
            with patch.dict(os.environ, {"PB_DAEMON_FETCH_BUDGET_S": "0"}):
                daemon_cli.main(["run", "--root", self.root, "--offline"])
            self.assertIsNone(seen["fetch_budget_s"])


class HandoffLockGroup(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def held_by_other(self, name):
        fh = (self.dir / (name + ".lock")).open("a")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh

    def free(self, name):
        with (self.dir / (name + ".lock")).open("a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False

    def test_waiting_never_holds_daily_lock(self):
        collector_fh = self.held_by_other("collector")
        observed = []

        def sleep(_):
            observed.append(self.free("daily"))      # daily.sh (flock -n) must get daily.lock while handoff waits
        with status.locks(self.dir, ("daily", "collector", "daemon"), 0.05, sleep=sleep) as ok:
            self.assertFalse(ok)
        self.assertTrue(observed and all(observed))
        collector_fh.close()

    def test_acquires_all_when_free_and_releases(self):
        with status.locks(self.dir, ("daily", "collector", "daemon"), 1) as ok:
            self.assertTrue(ok)
            self.assertFalse(self.free("collector"))
        self.assertTrue(all(self.free(n) for n in ("daily", "collector", "daemon")))

    def test_gets_group_once_other_holder_leaves(self):
        collector_fh = self.held_by_other("collector")

        def sleep(_):
            collector_fh.close()
        with status.locks(self.dir, ("daily", "collector", "daemon"), 10, sleep=sleep) as ok:
            self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
