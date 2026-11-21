"""Synthetic fixtures only: replay pre-gate promotion, then apply today's rules."""
import datetime as dt
import json
import unittest
from unittest.mock import patch

from helpers import NOW, add_call, add_outlet, add_source, service
from pbdata import facts
from pbdata.collect import metrics, promote, reconcile, store
from pbdata.collect.parse import Doc, Segment
from pbdata.daemon import project, stages
from pbdata.daemon.runner import Ledger, RunContext
from pbdata import db
from pbdata.model import FactInput
from pbdata.profiles import pitchboard as prof

LATER = "2026-11-08T00:00:00Z"
SCHOLARSHIP = "Student scholarship applications"
PERIOD = "Application period: October 1, 2026 through March 15, 2027."


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.infra = db.connect(":memory:")
        db.migrate(self.infra, "infra")
        self.addCleanup(self.svc.close)
        self.addCleanup(self.infra.close)
        self.outlet = add_outlet(self.svc)
        self.sources = 0

    def legacy_call(self, heading=SCHOLARSHIP, body=PERIOD, status=None):
        self.sources += 1
        sid = add_source(self.svc, "https://example.org/notice/%s" % self.sources, outlet=self.outlet)
        doc = Doc("html", segments=[Segment(0, "p", body, heading)])
        with patch.object(prof, "non_writing", return_value=False), \
                patch.object(prof, "has_writing_context", return_value=True), \
                patch.object(prof, "EXTRACTOR_VERSION", "synthetic-pre-gate"), \
                patch.object(prof, "LABELER_VERSION", "synthetic-pre-gate"):
            rev = store.ensure_revision(self.svc, sid, "h1", [], NOW, prof.EXTRACTOR_VERSION)
            raw = store.insert_raw(self.svc, NOW, source_id=sid, url="https://example.org/notice/%s" % self.sources,
                                   kind="html", fetch_method="html", outcome="ok", change_state="new", text_hash="h1", fetched_at=NOW)
            store.put_extractions(self.svc, raw, prof.extract(doc), "rules", prof.EXTRACTOR_VERSION, NOW)
            store.put_labels(self.svc, raw, prof.label_doc(doc, prof.extract(doc), dt.date.fromisoformat(NOW[:10])), prof.LABELER_VERSION, NOW)
            result = promote.promote_raw(self.svc, raw, NOW)
        self.assertEqual(result.calls_created, 1)
        cid = self.svc.execute("SELECT subject_id FROM pb_fact WHERE source_id=? AND subject_type='call' LIMIT 1", (sid,)).fetchone()[0]
        self.assertEqual(self.status(cid), "open")
        if status:
            # Set up other existing statuses without testing their original derivation.
            self.svc.execute("UPDATE pb_call_edition SET status=? WHERE call_id=?", (status, cid))
        return cid, sid, rev, raw

    def status(self, cid):
        return self.svc.execute("SELECT status FROM pb_call_edition WHERE call_id=?", (cid,)).fetchone()[0]

    def test_pre_gate_scholarship_corrected_with_audit_and_facts_unchanged(self):
        cid, sid, rev, raw = self.legacy_call()
        before = [tuple(r) for r in self.svc.execute("SELECT * FROM pb_fact ORDER BY fact_id")]
        result = reconcile.reconcile(self.svc, LATER)
        self.assertEqual(result["closed_nonwriting"], 1)
        self.assertEqual(self.status(cid), "closed")
        event = self.svc.execute("SELECT * FROM pb_status_event WHERE kind='correction'").fetchone()
        self.assertEqual((event["from_status"], event["to_status"], event["basis"]), ("open", "closed", "evidence"))
        self.assertTrue(event["evidence_id"])
        reason = json.loads(event["reason"])
        self.assertEqual(reason["extractor_version"], prof.EXTRACTOR_VERSION)
        self.assertEqual(reason["labeler_version"], prof.LABELER_VERSION)
        self.assertTrue(all(b["raw_id"] == raw and b["source_id"] == sid and b["revision_id"] == rev for b in reason["backing"]))
        self.assertEqual(before, [tuple(r) for r in self.svc.execute("SELECT * FROM pb_fact ORDER BY fact_id")])
        self.assertEqual(metrics.compute(self.svc, self.infra, {"targets": []})["open_calls_current"], 0)
        project.recompute(self.svc, LATER)
        self.assertEqual(json.loads(self.svc.execute("SELECT payload FROM pb_search_projection WHERE call_id=?", (cid,)).fetchone()[0])["applicable_now"], "no")

    def test_true_writing_contest_and_literary_scholarship_stay(self):
        for heading in ("Poetry contest", "Poetry scholarship applications", "문학 원고 공모"):
            with self.subTest(heading=heading):
                cid, *_ = self.legacy_call(heading, PERIOD.replace("March 15", "March %s" % (15 + self.sources)))
                reconcile.reconcile(self.svc, LATER)
                self.assertEqual(self.status(cid), "open")

    def test_ambiguous_and_missing_evidence_stay(self):
        cid, *_ = self.legacy_call("Application guidelines")
        missing = add_call(self.svc, self.outlet, key="missing", status="announced")
        reconcile.reconcile(self.svc, LATER)
        self.assertEqual(self.status(cid), "open")
        self.assertEqual(self.status(missing), "announced")

    def test_idempotent(self):
        self.legacy_call()
        reconcile.reconcile(self.svc, LATER)
        counts = [self.svc.execute("SELECT count(*) FROM " + t).fetchone()[0] for t in ("pb_fact", "pb_status_event", "pb_evidence", "pb_label")]
        self.assertEqual(reconcile.reconcile(self.svc, LATER)["changes"], [])
        self.assertEqual(counts, [self.svc.execute("SELECT count(*) FROM " + t).fetchone()[0] for t in ("pb_fact", "pb_status_event", "pb_evidence", "pb_label")])

    def test_current_full_page_label_retains_discarded_title_context(self):
        cid, _, _, raw = self.legacy_call("Application guidelines")
        doc = Doc("html", title=SCHOLARSHIP, segments=[Segment(0, "p", PERIOD)])
        labs = prof.label_doc(doc, prof.extract(doc), dt.date.fromisoformat(LATER[:10]))
        store.put_labels(self.svc, raw, labs, prof.LABELER_VERSION, LATER)
        self.assertEqual(reconcile.reconcile(self.svc, LATER)["closed_nonwriting"], 1)
        self.assertEqual(self.status(cid), "closed")

    def test_old_full_page_rejection_is_not_used(self):
        cid, _, _, raw = self.legacy_call("Application guidelines")
        doc = Doc("html", title=SCHOLARSHIP, segments=[Segment(0, "p", PERIOD)])
        store.put_labels(self.svc, raw, prof.label_doc(doc, prof.extract(doc), dt.date.fromisoformat(LATER[:10])), "old-labeler", LATER)
        self.assertEqual(reconcile.reconcile(self.svc, LATER)["closed_nonwriting"], 0)
        self.assertEqual(self.status(cid), "open")

    def test_current_eligible_full_page_label_keeps_ambiguous_local_basis(self):
        cid, _, _, raw = self.legacy_call()
        doc = Doc("html", title="Poetry scholarship", segments=[Segment(0, "p", PERIOD)])
        store.put_labels(self.svc, raw, prof.label_doc(doc, prof.extract(doc), dt.date.fromisoformat(LATER[:10])), prof.LABELER_VERSION, LATER)
        self.assertEqual(reconcile.reconcile(self.svc, LATER)["closed_nonwriting"], 0)
        self.assertEqual(self.status(cid), "open")

    def test_writing_context_elsewhere_keeps_finance_call(self):
        cid, sid, rev, _ = self.legacy_call()
        facts.add_evidence(self.svc, rev, sid, "Submit an original poetry manuscript.", "", 1, 0, NOW)
        self.assertEqual(reconcile.reconcile(self.svc, LATER)["closed_nonwriting"], 0)
        self.assertEqual(self.status(cid), "open")

    def test_literary_heading_elsewhere_preserves_a_possible_title_override(self):
        cid, sid, rev, _ = self.legacy_call()
        facts.add_evidence(self.svc, rev, sid, "Eligibility guidelines.", "Poetry scholarship", 1, 0, NOW)
        self.assertEqual(reconcile.reconcile(self.svc, LATER)["closed_nonwriting"], 0)
        self.assertEqual(self.status(cid), "open")

    def test_one_ambiguous_backing_source_keeps_call(self):
        cid, *_ = self.legacy_call()
        sid = add_source(self.svc, "https://example.org/second", outlet=self.outlet)
        rev = store.ensure_revision(self.svc, sid, "h2", [], NOW, "test")
        ev = facts.add_evidence(self.svc, rev, sid, "Deadline: March 15, 2027.", "", 0, 0, NOW)
        facts.append(self.svc, FactInput("call", cid, "deadline.closes_on", sid, "stated", "source_claim", NOW, "rules", "test",
                                       value={"date": "2027-03-15", "tz_source": "unknown"}, evidence_id=ev), NOW)
        self.assertEqual(reconcile.reconcile(self.svc, LATER)["closed_nonwriting"], 0)
        self.assertEqual(self.status(cid), "open")

    def test_unstated_gap_marker_is_not_a_promoting_basis(self):
        cid, *_ = self.legacy_call()
        sid = add_source(self.svc, "https://example.org/guidelines", outlet=self.outlet)
        rev = store.ensure_revision(self.svc, sid, "h2", [], NOW, "test")
        facts.append(self.svc, FactInput("call", cid, "status.declared", sid, "not_stated_in_inspected_scope", "source_claim",
                                       NOW, "rules", "test", inspected_revision_id=rev), NOW)
        self.assertEqual(reconcile.reconcile(self.svc, LATER)["closed_nonwriting"], 1)
        self.assertEqual(self.status(cid), "closed")

    def test_unchanged_fetch_reuses_same_content_evidence(self):
        cid, sid, _, _ = self.legacy_call()
        store.insert_raw(self.svc, LATER, source_id=sid, url="https://example.org/notice", kind="html", fetch_method="html",
                         outcome="ok", change_state="unchanged", text_hash="h1", fetched_at=LATER)
        self.assertEqual(reconcile.reconcile(self.svc, LATER)["closed_nonwriting"], 1)
        self.assertEqual(self.status(cid), "closed")

    def test_newer_content_without_the_backing_excerpt_keeps_call(self):
        cid, sid, _, _ = self.legacy_call()
        store.ensure_revision(self.svc, sid, "h2", [], LATER, "test")
        store.insert_raw(self.svc, LATER, source_id=sid, url="https://example.org/notice", kind="html", fetch_method="html",
                         outcome="ok", change_state="changed", text_hash="h2", fetched_at=LATER)
        self.assertEqual(reconcile.reconcile(self.svc, LATER)["closed_nonwriting"], 0)
        self.assertEqual(self.status(cid), "open")

    def test_daily_state_stage_reconciles_all_live_statuses_only(self):
        for status in ("announced", "open", "extended", "standing", "candidate", "closed", "cancelled", "status_unknown", "superseded", "archived"):
            # A separate outlet prevents promotion reusing an existing edition.
            self.outlet = add_outlet(self.svc)
            cid, *_ = self.legacy_call(status=status)
            ctx = RunContext(self.svc, self.infra, "unused", "test", lambda: LATER, Ledger(self.infra, LATER[:10]), {})
            result = stages.s_state_update(ctx)
            self.assertEqual(self.status(cid), "closed" if status in reconcile.ACTIVE_STATUSES else status)
            self.assertEqual(result["closed_nonwriting"], int(status in reconcile.ACTIVE_STATUSES))


if __name__ == "__main__":
    unittest.main()
