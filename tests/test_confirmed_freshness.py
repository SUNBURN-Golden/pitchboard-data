import dataclasses
import json
import unittest
from unittest.mock import patch

from pbdata import facts, timeutil
from pbdata.daemon import project, questions, stages
from pbdata.model import FactInput
from helpers import add_call
from test_daemon import Env


class ConfirmedFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.e = Env(now="2026-11-13T00:00:00Z")
        self.addCleanup(self.e.close)
        e = self.e
        self.call = add_call(e.svc, e.out, key="2026-12-01", status="open")
        collected = "2026-11-01T00:00:00Z"
        ev = facts.add_evidence(e.svc, e.rev, e.sid, "Deadline: December 1, 2026", "Guidelines", 1, 0, collected)
        self.input = FactInput(
            "call", self.call, "deadline.closes_on", e.sid, "stated", "verified_fact",
            collected, "rules", "t", value={"date": "2026-12-01", "tz_source": "unknown"},
            evidence_id=ev, inspected_revision_id=e.rev, source_authority="official_primary",
            verified_at=collected, verifier="rules", fresh_until="2026-11-08T00:00:00Z")
        self.fid = facts.append(e.svc, self.input, collected).fact_id

    def confirm(self, collected):
        e = self.e
        rev = e.rev
        ev = facts.add_evidence(e.svc, rev, e.sid, "Deadline: December 1, 2026", "Guidelines", 1, 0, collected)
        result = facts.append(e.svc, dataclasses.replace(
            self.input, collected_at=collected, evidence_id=ev, inspected_revision_id=rev), collected)
        self.assertEqual(result.action, "unchanged")
        self.assertEqual(result.fact_id, self.fid)

    def check_freshness(self, stale, fresh_until):
        e = self.e
        with patch.object(timeutil, "utcnow", return_value=timeutil.parse_iso(e.now)):
            project.recompute(e.svc, e.now)
        payload = json.loads(e.svc.execute("SELECT payload FROM pb_search_projection WHERE call_id=?", (self.call,)).fetchone()[0])
        view = payload["conditions"]["deadline.closes_on"]["facts"][0]
        self.assertEqual(view["stale"], stale)
        self.assertEqual(view["fresh_until"], fresh_until)
        self.assertEqual(payload["applicable_now"], "unknown" if stale else "yes")
        self.assertEqual(len(questions.answer_all(e.svc)["Q1_open_now"]), 0 if stale else 1)
        self.assertEqual(stages.s_evidence_verification(e.ctx())["verified_stale_needs_recheck"], int(stale))
        self.assertEqual(stages.s_quality_check(e.ctx())["stale_live_facts"], int(stale))
        self.assertEqual(e.svc.execute("SELECT fresh_until FROM pb_fact WHERE fact_id=?", (self.fid,)).fetchone()[0],
                         "2026-11-08T00:00:00Z")

    def test_later_confirmation_keeps_open_call_fresh(self):
        self.confirm("2026-11-12T00:00:00Z")
        self.check_freshness(False, "2026-11-19T00:00:00Z")

    def test_without_confirmation_remains_stale(self):
        self.check_freshness(True, "2026-11-08T00:00:00Z")

    def test_expired_confirmation_remains_stale(self):
        self.confirm("2026-11-04T00:00:00Z")
        self.check_freshness(True, "2026-11-11T00:00:00Z")
