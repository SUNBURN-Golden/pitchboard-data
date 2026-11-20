import json
import os
import shutil
import tempfile
import unittest

from pbdata import db, facts, statemachine
from pbdata.model import FactInput
from pbdata.daemon import project, questions, clips
from pbdata.daemon.runner import DaemonLock, Ledger, RunContext, StageRunner
from pbdata.daemon import stages as S
from helpers import add_outlet, add_source, add_call, add_revision

T0 = "2026-10-20T00:00:00Z"


def fact(svc, sid, rev, subject, pred, value, text, now, stype="call", state="stated"):
    ev = facts.add_evidence(svc, rev, sid, text, "Guidelines", 1, 0, now)
    return facts.append(svc, FactInput(stype, subject, pred, sid, state, "source_claim", now, "rules", "t", value=value,
                                       evidence_id=ev, inspected_revision_id=rev, source_authority="official_primary"), now)


class Env:
    def __init__(self, now=T0):
        self.root = tempfile.mkdtemp()
        for d in ("service_db", "infra_db", "locks", "exports"):
            os.makedirs(os.path.join(self.root, d))
        self.svc = db.open_service(os.path.join(self.root, "service_db", "s.sqlite3"))
        self.infra = db.open_infra(os.path.join(self.root, "infra_db", "i.sqlite3"))
        self.now = now
        self.out = add_outlet(self.svc, "Example Review")
        self.sid = add_source(self.svc, outlet=self.out)
        self.rev = add_revision(self.svc, self.sid, fetched_at=now)

    def ctx(self, run_id="r1", **opts):
        o = {"offline": True, "targets": {"targets": []}}
        o.update(opts)
        c = RunContext(self.svc, self.infra, self.root, run_id, lambda: self.now, Ledger(self.infra, self.now[:10]), o)
        c.log = lambda *_: None
        return c

    def close(self):
        shutil.rmtree(self.root)


class PropagationTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.close()

    def test_correction_reaches_projection_and_history_kept(self):
        e = self.e
        call = add_call(e.svc, e.out, key="2026-11-01", scope="book_review")
        r1 = fact(e.svc, e.sid, e.rev, call, "deadline.closes_on", {"date": "2026-11-01", "tz_source": "unknown"}, "Deadline: November 1, 2026", e.now)
        statemachine.transition(e.svc, call, "open", "evidence", e.now, "t", "open", fact_id=r1.fact_id)
        StageRunner(e.ctx("r1"), S.STAGES).run()
        p1 = json.loads(e.svc.execute("SELECT payload FROM pb_search_projection WHERE call_id=?", (call,)).fetchone()[0])
        self.assertEqual(p1["closes_on"], "2026-11-01")
        self.assertEqual(p1["applicable_now"], "yes")
        # source corrects the deadline (extension)
        rev2 = add_revision(e.svc, e.sid, content_hash="h2", fetched_at="2026-10-21T00:00:00Z")
        e.now = "2026-10-21T00:00:00Z"
        r2 = fact(e.svc, e.sid, rev2, call, "deadline.closes_on", {"date": "2026-11-15", "tz_source": "unknown"}, "Deadline extended to November 15, 2026", e.now)
        statemachine.transition(e.svc, call, "extended", "evidence", e.now, "t", "ext", fact_id=r2.fact_id)
        rep = StageRunner(e.ctx("r2"), S.STAGES).run()
        row = e.svc.execute("SELECT payload, projection_version FROM pb_search_projection WHERE call_id=?", (call,)).fetchone()
        p2 = json.loads(row[0])
        self.assertEqual(p2["closes_on"], "2026-11-15")
        self.assertEqual(p2["status"], "extended")
        self.assertGreaterEqual(row[1], 2)
        self.assertEqual(rep["stages"]["quality_check"]["status"], "done")
        q = e.ctx().infra.execute("SELECT metrics FROM metric_snapshot ORDER BY taken_at DESC LIMIT 1").fetchone()[0]
        self.assertEqual(json.loads(q)["projection_points_to_superseded"], 0)
        hist = facts.fact_history(e.svc, call, "deadline.closes_on")
        self.assertEqual(len(hist), 2)                      # old value kept in history
        self.assertEqual(json.loads(e.svc.execute("SELECT count(*) FROM pb_outbox WHERE kind='projection_updated'").fetchone()[0].__str__()), 2)

    def test_clock_closes_but_never_cancels(self):
        e = self.e
        call = add_call(e.svc, e.out, key="2026-10-01")
        r = fact(e.svc, e.sid, e.rev, call, "deadline.closes_on", {"date": "2026-10-01", "tz_source": "unknown"}, "Deadline: October 1, 2026", "2026-09-01T00:00:00Z")
        statemachine.transition(e.svc, call, "open", "evidence", "2026-09-01T00:00:00Z", "t", "o", fact_id=r.fact_id)
        out = S.s_state_update(e.ctx())
        self.assertEqual(out["closed_by_clock"], 1)
        st = e.svc.execute("SELECT status, status_basis FROM pb_call_edition WHERE call_id=?", (call,)).fetchone()
        self.assertEqual(tuple(st), ("closed", "system_clock"))
        self.assertEqual(out["cancelled_by_evidence"], 0)

    def test_cancel_needs_evidence(self):
        e = self.e
        call = add_call(e.svc, e.out, key="2026-12-01")
        r = fact(e.svc, e.sid, e.rev, call, "deadline.closes_on", {"date": "2026-12-01", "tz_source": "unknown"}, "Deadline: December 1, 2026", "2026-10-01T00:00:00Z")
        statemachine.transition(e.svc, call, "open", "evidence", "2026-10-01T00:00:00Z", "t", "o", fact_id=r.fact_id)
        fact(e.svc, e.sid, e.rev, call, "status.declared", {"declared": "cancelled"}, "This contest has been cancelled.", e.now)
        out = S.s_state_update(e.ctx())
        self.assertEqual(out["cancelled_by_evidence"], 1)
        S.s_service_recompute(e.ctx())
        p = json.loads(e.svc.execute("SELECT payload FROM pb_search_projection WHERE call_id=?", (call,)).fetchone()[0])
        self.assertEqual(p["applicable_now"], "no")

    def test_missing_condition_is_unknown_not_unrestricted(self):
        e = self.e
        call = add_call(e.svc, e.out, key="standing")
        p = project.build_payload(e.svc, call)
        for pred in ("fee.submission", "eligibility.region", "compensation.rate", "rights.simultaneous"):
            self.assertEqual(p["conditions"][pred]["answer"], "unknown")
            self.assertIn("NOT 'no restriction'", p["conditions"][pred]["reason"])

    def test_questions_three_valued(self):
        e = self.e
        call = add_call(e.svc, e.out, key="2026-11-30")
        r = fact(e.svc, e.sid, e.rev, call, "deadline.closes_on", {"date": "2026-11-30", "tz_source": "unknown"}, "Deadline: November 30, 2026", e.now)
        statemachine.transition(e.svc, call, "open", "evidence", e.now, "t", "o", fact_id=r.fact_id)
        project.recompute(e.svc, e.now)
        a = questions.answer_all(e.svc)
        self.assertEqual(len(a["Q1_open_now"]), 1)
        self.assertEqual(a["Q3_no_fee"][0]["no_fee"], "unknown")
        self.assertEqual(a["Q1_open_now"][0]["bucket"], "적합 가능(미확인 있음)")


class ClipTest(unittest.TestCase):
    def test_manual_clip_is_claim_only_and_unresolved_kept(self):
        e = Env()
        try:
            d = os.path.join(e.root, "inbox", "manual_clips")
            os.makedirs(d)
            json.dump({"url": "https://www.instagram.com/p/abc/", "clipped_by": "human:op", "clipped_at": e.now, "outlet_name": "Example Review",
                       "text": "Submissions are now open.\nDeadline: November 30, 2026.\nNo submission fee."}, open(os.path.join(d, "a.json"), "w"))
            json.dump({"url": "https://www.instagram.com/p/zzz/", "clipped_by": "human:op", "outlet_name": "Unknown Mag", "text": "Deadline: November 30, 2026"},
                      open(os.path.join(d, "b.json"), "w"))
            out = clips.ingest_dir(e.svc, e.root, e.now)
            self.assertEqual(out["clips_ingested"], 1)
            self.assertEqual(out["clips_unresolved"], 1)
            self.assertGreater(out["clip_facts"], 0)
            epi = {r[0] for r in e.svc.execute("SELECT f.epistemic FROM pb_fact f JOIN pb_source s ON s.source_id=f.source_id WHERE s.source_type='manual_clip'")}
            self.assertEqual(epi, {"source_claim"})
            self.assertTrue(os.path.exists(os.path.join(d, "unresolved", "b.json")))
        finally:
            e.close()


class RunnerTest(unittest.TestCase):
    def test_retry_failure_queue_and_resume(self):
        e = Env()
        try:
            calls = {"n": 0}
            def flaky(c):
                calls["n"] += 1
                if calls["n"] <= 3:
                    raise IOError("boom")
                return {"ok": 1}
            done = []
            st = [("a", lambda c: done.append("a") or {"x": 1}), ("b", flaky), ("c", lambda c: done.append("c") or {})]
            rep = StageRunner(e.ctx("rr"), st, sleep=lambda s: None).run()
            self.assertEqual(rep["stopped"], "failed_at:b")
            self.assertEqual(e.infra.execute("SELECT attempts FROM failure_queue WHERE stage='b'").fetchone()[0], 3)
            rep2 = StageRunner(e.ctx("rr"), st, sleep=lambda s: None).run()      # resume: a is skipped, b retried, c runs
            self.assertNotIn("stopped", rep2)
            self.assertEqual(rep2["stages"]["a"]["status"], "skipped_done")
            self.assertEqual(done, ["a", "c"])
        finally:
            e.close()

    def test_ai_promotion_is_disabled_even_with_unlimited_ledger(self):
        e = Env()
        try:
            rep = StageRunner(e.ctx("rc", ai_extraction=True), [("extraction", S.s_extraction)], sleep=lambda s: None).run()
            self.assertEqual(rep["stages"]["extraction"]["status"], "failed")
            self.assertEqual(rep["stages"]["extraction"]["error"], "AI extraction is disabled for promotion")
            self.assertIsNone(e.infra.execute("SELECT amount FROM cost_ledger WHERE resource='model_calls'").fetchone())
        finally:
            e.close()

    def test_ledger_logs_counts_tokens_and_cost_without_caps(self):
        e = Env()
        try:
            ledger = e.ctx().ledger
            for resource, amount in (("model_calls", 60), ("model_tokens", 1_000_000), ("model_usd", 12.5)):
                ledger.charge(resource, amount)
                ledger.charge(resource, amount)
                self.assertEqual(ledger.used(resource), amount * 2)
        finally:
            e.close()

    def test_lock_excludes_second_run(self):
        e = Env()
        try:
            p = os.path.join(e.root, "locks", "daemon.lock")
            with DaemonLock(p):
                with self.assertRaises(RuntimeError):
                    with DaemonLock(p):
                        pass
        finally:
            e.close()

    def test_kill_switch(self):
        e = Env()
        try:
            open(os.path.join(e.root, "locks", "KILL_SWITCH"), "w").close()
            rep = StageRunner(e.ctx("rk"), S.STAGES).run()
            self.assertEqual(rep["stopped"], "kill_switch")
        finally:
            e.close()


class WindowSincePrevRun(unittest.TestCase):
    def test_window_starts_at_previous_ok_daemon_run(self):
        e = Env()
        e.infra.execute("INSERT INTO runs(run_id,started_at,finished_at,outcome) VALUES('dmn_old','2026-10-19T21:00:00Z','2026-10-19T21:01:00Z','ok')")
        e.infra.execute("INSERT INTO runs(run_id,started_at,outcome) VALUES('dmn_bad','2026-10-19T22:00:00Z','failed')")
        e.infra.execute("INSERT INTO runs(run_id,started_at) VALUES('dmn_now','2026-10-20T00:00:00Z')")
        e.infra.commit()
        c = e.ctx(run_id="dmn_now")
        self.assertEqual(S.s_discovery(c)["window_start"], "2026-10-19T21:00:00Z")

    def test_window_defaults_to_clock_without_history(self):
        e = Env()
        self.assertEqual(S.s_discovery(e.ctx(run_id="dmn_first"))["window_start"], T0)


if __name__ == "__main__":
    unittest.main()
