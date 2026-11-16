import contextlib
import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import NOW, add_call, add_revision, add_source, service
from pbdata import db, facts
from pbdata.collect import collector, scheduler, store
from pbdata.model import FactInput


REFRESH_NOW = "2026-11-16T00:00:00Z"
STALE = "2026-11-15T00:00:00Z"
FUTURE = "2026-12-01T00:00:00Z"


class RefreshLiveCallsTests(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.inf = db.connect(":memory:")
        self.addCleanup(self.svc.close)
        self.addCleanup(self.inf.close)
        db.migrate(self.inf, "infra")
        self.site = store.register_site(self.svc, "https://refresh.example", "Refresh Review", NOW, target_id="t1")
        self.outlet = store.site_row(self.svc, self.site)["site_outlet"]
        self.ctx = collector.Ctx(self.svc, self.inf, None, None, {"targets": []}, "run_refresh", "refresh", lambda: REFRESH_NOW)

    def source(self, name, fetched_at="2026-11-14T00:00:00Z", kind="html"):
        sid = add_source(self.svc, "https://refresh.example/" + name, outlet=self.outlet)
        self.raw(sid, fetched_at=fetched_at, kind=kind)
        return sid

    def url(self, sid):
        return self.svc.execute("SELECT canonical_url FROM pb_source WHERE source_id=?", (sid,)).fetchone()[0]

    def raw(self, sid, url=None, fetched_at=REFRESH_NOW, kind="html", depth=1, outcome="ok"):
        return store.insert_raw(self.svc, fetched_at, source_id=sid, url=url or self.url(sid), kind=kind,
                                fetch_method="html", outcome=outcome, change_state="new" if outcome == "ok" else "failed",
                                depth=depth, fetched_at=fetched_at)

    def fact(self, sid, status="open", predicate="deadline.closes_on", date="2026-12-01", fresh_until=STALE, call=None):
        call = call or add_call(self.svc, self.outlet, key=sid, status=status)
        rev = add_revision(self.svc, sid, content_hash=predicate)
        excerpt = "Deadline: %s." % date if predicate == "deadline.closes_on" else "Submissions are open."
        ev = facts.add_evidence(self.svc, rev, sid, excerpt, "Guidelines", 0, 0, NOW)
        value = {"date": date, "tz_source": "unknown"} if predicate == "deadline.closes_on" else {"status": "open"}
        f = FactInput("call", call, predicate, sid, "stated", "verified_fact", NOW, "rules", "1",
                      value=value, evidence_id=ev, inspected_revision_id=rev, source_authority="official_primary",
                      verifier="rules", verified_at=NOW, fresh_until=fresh_until)
        return facts.append(self.svc, f, NOW).fact_id, f

    def jobs(self):
        return [(json.loads(r["payload"]), r) for r in self.inf.execute("SELECT * FROM job ORDER BY rowid")]

    def selected(self):
        return [p["url"] for p, _ in self.jobs()]

    def test_recently_fetched_stale_live_source_precedes_oldest(self):
        old = self.source("old", "2026-10-01T00:00:00Z")
        live = self.source("live")
        self.fact(live)
        self.assertEqual(collector.enqueue_refresh(self.ctx, 1), 1)
        self.assertEqual(self.selected(), [self.url(live)])
        self.assertNotIn(self.url(old), self.selected())
        payload, job = self.jobs()[0]
        self.assertEqual(payload, {"url": self.url(live), "site_source_id": self.site, "depth": 1, "via": "refresh"})
        self.assertEqual(job["idem_key"], "fetch|%s|refresh" % self.url(live))
        self.assertEqual(job["budget_class"], "freshness")
        self.assertEqual(job["priority"], scheduler.priority(novelty=.1, urgency=1, quality=.5, cost=.2))

    def test_closed_superseded_calls_past_deadlines_and_superseded_facts_not_prioritized(self):
        old = self.source("old", "2026-10-01T00:00:00Z")
        for status in ("closed", "superseded", "cancelled", "candidate", "archived"):
            self.fact(self.source(status), status=status)
        self.fact(self.source("past"), date="2026-11-15")
        sid = self.source("superseded-fact")
        fid, f = self.fact(sid)
        replacement = dataclasses.replace(f, value={"date": "2026-12-31", "tz_source": "unknown"}, fresh_until=FUTURE)
        result = facts.append(self.svc, replacement, NOW)
        self.assertIn(fid, result.superseded)
        self.assertEqual(collector.enqueue_refresh(self.ctx, 1), 1)
        self.assertEqual(self.selected(), [self.url(old)])

    def test_fresh_live_source_stays_in_round_robin(self):
        old = self.source("old", "2026-10-01T00:00:00Z")
        self.fact(self.source("fresh"), fresh_until=FUTURE)
        self.assertEqual(collector.enqueue_refresh(self.ctx, 1), 1)
        self.assertEqual(self.selected(), [self.url(old)])

    def test_confirmation_extends_effective_freshness(self):
        old = self.source("old", "2026-10-01T00:00:00Z")
        sid = self.source("confirmed")
        fid, f = self.fact(sid)
        result = facts.append(self.svc, dataclasses.replace(f, collected_at=REFRESH_NOW), REFRESH_NOW)
        self.assertEqual(result.action, "unchanged")
        self.assertGreater(facts.effective_fresh_until(self.svc, fid), "2026-11-18T00:00:00Z")
        self.assertEqual(collector.enqueue_refresh(self.ctx, 1), 1)
        self.assertEqual(self.selected(), [self.url(old)])

    def test_limit_and_dedup_across_facts_raws_and_round_robin(self):
        live = self.source("live", "2026-10-01T00:00:00Z")
        _, f = self.fact(live)
        self.fact(live, predicate="status.declared", call=f.subject_id)
        self.raw(live)
        old = self.source("old", "2026-10-02T00:00:00Z")
        self.source("extra", "2026-10-03T00:00:00Z")
        self.assertEqual(collector.enqueue_refresh(self.ctx, 2), 2)
        self.assertEqual(self.selected(), [self.url(live), self.url(old)])
        self.assertEqual(len(set(self.selected())), 2)
        # Existing run-tag keys also cannot create duplicate jobs.
        self.assertEqual(collector.enqueue_refresh(self.ctx, 10), 1)
        self.assertEqual(len(self.selected()), 3)
        self.assertEqual(collector.enqueue_refresh(self.ctx, 10), 0)

    def test_live_status_facts_all_live_states_and_kinds(self):
        for status, kind in (("announced", "html"), ("open", "pdf"), ("extended", "image"), ("standing", "html")):
            with self.subTest(status=status, kind=kind):
                sid = self.source(status, kind=kind)
                self.fact(sid, status=status, predicate="status.declared")
        candidates = collector.refresh_candidates(self.svc, REFRESH_NOW)
        self.assertEqual(len(candidates), 4)
        self.assertTrue(all(r["refresh_fresh_until"] == STALE for r in candidates))

    def test_horizon_boundary_soonest_first_and_url_ties(self):
        old = self.source("old", "2026-10-01T00:00:00Z")
        for name, expiry in (("boundary", "2026-11-18T00:00:00Z"), ("stale-b", STALE),
                             ("stale-a", STALE), ("outside", "2026-11-18T00:00:01Z")):
            self.fact(self.source(name), fresh_until=expiry)
        expected = ["https://refresh.example/" + name for name in ("stale-a", "stale-b", "boundary")]
        candidates = collector.refresh_candidates(self.svc, REFRESH_NOW)
        self.assertEqual([r["url"] for r in candidates[:3]], expected)
        self.assertEqual(candidates[3]["url"], self.url(old))
        self.assertEqual(candidates, collector.refresh_candidates(self.svc, REFRESH_NOW))
        with patch.dict(os.environ, {"PB_REFRESH_HORIZON_HOURS": "0"}):
            self.assertEqual(collector.enqueue_refresh(self.ctx, 3), 3)
        self.assertEqual(self.selected(), expected[:2] + [self.url(old)])

    def test_canonical_url_preferred_over_other_raw_urls(self):
        sid = self.source("canonical")
        self.raw(sid, url="https://refresh.example/alias", fetched_at="2026-10-01T00:00:00Z")
        self.fact(sid)
        self.assertEqual(collector.enqueue_refresh(self.ctx, 1), 1)
        self.assertEqual(self.selected(), [self.url(sid)])

    def test_other_source_urls_used_if_canonical_has_no_ok_raw(self):
        sid = add_source(self.svc, "https://refresh.example/canonical", outlet=self.outlet)
        self.raw(sid, kind="json")
        self.raw(sid, outcome="error")
        for path in ("alias-b", "alias-a"):
            self.raw(sid, url="https://refresh.example/" + path)
        self.fact(sid)
        self.assertEqual(collector.enqueue_refresh(self.ctx, 2), 2)
        self.assertEqual(self.selected(), ["https://refresh.example/alias-a", "https://refresh.example/alias-b"])

    def test_owner_lookup_required_and_latest_ok_depth_used(self):
        orphan = add_source(self.svc, "https://orphan.example/call")
        self.raw(orphan)
        self.fact(orphan, fresh_until="2026-11-01T00:00:00Z")
        live = self.source("live")
        self.raw(live, depth=2)
        self.raw(live, fetched_at="2026-11-17T00:00:00Z", depth=3, outcome="error")
        self.fact(live)
        self.assertEqual(collector.enqueue_refresh(self.ctx, 1), 1)
        self.assertEqual(self.selected(), [self.url(live)])
        self.assertEqual(self.jobs()[0][0]["depth"], 2)

    def test_zero_limit(self):
        self.fact(self.source("live"))
        self.assertEqual(collector.enqueue_refresh(self.ctx, 0), 0)
        self.assertEqual(self.selected(), [])

    def test_eval_compares_top_40_on_temporary_copy_without_infra(self):
        for i in range(40):
            self.source("old-%02d" % i, "2026-10-01T00:00:00Z")
        live = self.source("live", REFRESH_NOW)
        self.fact(live)
        script = Path(__file__).resolve().parents[1] / "scripts" / "eval_refresh_live_calls.py"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.sqlite3"
            with contextlib.closing(sqlite3.connect(str(path))) as dest:
                self.svc.backup(dest)
            before = path.read_bytes()
            for args in ([], ["--now", REFRESH_NOW]):
                with self.subTest(args=args):
                    result = subprocess.run([sys.executable, str(script), str(path)] + args,
                                            check=True, capture_output=True, text=True)
                    report = json.loads(result.stdout)
                    self.assertEqual(report["now"], REFRESH_NOW)
                    self.assertEqual(report["live_call_urls_needing_refresh"], [self.url(live)])
                    self.assertEqual(report["old_selection_live_call_urls_included"], 0)
                    self.assertEqual(report["new_selection_live_call_urls_included"], 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [path.name])


if __name__ == "__main__":
    unittest.main()
