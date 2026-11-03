"""Call projection must not infer page applicability from shared site ownership."""
import unittest

from helpers import NOW, add_call, add_outlet, add_revision, add_source, service
from pbdata import facts
from pbdata.daemon import project
from pbdata.model import FactInput


class ProjectionSourcesTest(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.addCleanup(self.svc.close)
        self.outlet = add_outlet(self.svc)
        self.call = add_call(self.svc, self.outlet, key="2026-12-01", status="open")
        self.page = self.source("/competitions/poetry")
        self.article = self.source("/articles/writers-income-faq")
        self.guidelines = self.source("/competitions/poetry/rules.pdf")
        self.put(self.page, "deadline.closes_on", {"date": "2026-12-01", "tz_source": "unknown"}, call=True)

    def source(self, path):
        sid = add_source(self.svc, "https://example.org" + path, outlet=self.outlet)
        return sid, add_revision(self.svc, sid)

    def put(self, source, pred, value, call=False, epistemic="source_claim", state="stated"):
        sid, rev = source
        ev = facts.add_evidence(self.svc, rev, sid, "Stated guidelines evidence", "Rules", 1, 0, NOW)
        return facts.append(self.svc, FactInput(
            "call" if call else "outlet", self.call if call else self.outlet,
            pred, sid, state, epistemic, NOW, "rules", "test", value=value,
            evidence_id=ev, inspected_revision_id=rev, source_authority="official_primary"), NOW).fact_id

    def payload(self):
        return project.build_payload(self.svc, self.call)

    def test_off_call_official_guidelines_fact_is_not_inherited(self):
        self.put(self.article, "compensation.rate", {"state": "reported_income"})
        before = self.svc.execute("SELECT count(*) FROM pb_fact").fetchone()[0]
        p = self.payload()
        self.assertEqual(p["conditions"]["compensation.rate"]["answer"], "unknown")
        self.assertEqual(p["conditions"]["compensation.rate"]["facts"], [])
        self.assertNotIn(self.article[1], p["source_revision_ids"])
        self.assertEqual(self.svc.execute("SELECT count(*) FROM pb_fact").fetchone()[0], before)

    def test_same_page_true_fact_survives_unrelated_outlet_conflict(self):
        kept = self.put(self.page, "compensation.rate", {"state": "prize"})
        self.put(self.article, "compensation.rate", {"state": "reported_income"})
        p = self.payload()
        cell = p["conditions"]["compensation.rate"]
        self.assertEqual(cell["answer"], "stated")
        self.assertEqual([f["fact_id"] for f in cell["facts"]], [kept])
        self.assertEqual(p["source_revision_ids"], [self.page[1]])

    def test_explicit_separate_guidelines_source_keeps_true_conflicts(self):
        self.put(self.guidelines, "status.declared", {"declared": "open"}, call=True)
        ids = {self.put(s, "compensation.rate", {"state": v}) for s, v in
               ((self.page, "prize"), (self.guidelines, "other_prize"))}
        p = self.payload()
        cell = p["conditions"]["compensation.rate"]
        self.assertEqual(cell["answer"], "conflict")
        self.assertEqual({f["fact_id"] for f in cell["facts"]}, ids)
        self.assertEqual(set(p["source_revision_ids"]), {self.page[1], self.guidelines[1]})

    def test_call_fact_still_overrides_outlet_fact(self):
        self.put(self.page, "compensation.rate", {"state": "outlet_rate"})
        kept = self.put(self.guidelines, "compensation.rate", {"state": "call_prize"}, call=True)
        cell = self.payload()["conditions"]["compensation.rate"]
        self.assertEqual(cell["from"], "call")
        self.assertEqual([f["fact_id"] for f in cell["facts"]], [kept])

    def test_inferred_or_missing_relation_cannot_enable_inheritance(self):
        self.put(self.article, "status.declared", {"declared": "open"}, call=True, epistemic="ai_inference")
        self.put(self.article, "compensation.rate", {"state": "reported_income"})
        self.assertEqual(self.payload()["conditions"]["compensation.rate"]["answer"], "unknown")
        # A call without any evidence-backed source must also fail closed.
        self.call = add_call(self.svc, self.outlet, key="2027-01-01", status="open")
        self.assertEqual(self.payload()["conditions"]["compensation.rate"]["answer"], "unknown")


if __name__ == "__main__":
    unittest.main()
