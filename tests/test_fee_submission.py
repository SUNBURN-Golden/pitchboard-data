"""Synthetic fee-payment statements; no collected pages or domain-specific rules."""
import datetime as dt
import json
import unittest

from helpers import NOW, add_outlet, add_source, service
from pbdata.collect import gaps, promote, store
from pbdata.collect.parse import Doc, Segment
from pbdata.profiles import pitchboard as prof


def fee_doc(text, heading="", title="Submission guidelines"):
    return Doc("html", title=title, segments=[Segment(0, "p", text, heading)])


def fees(text, **kw):
    return [e for e in prof.extract(fee_doc(text, **kw)) if e.field == "fee.submission"]


class FeeSubmissionTests(unittest.TestCase):
    def test_payment_instructions_state_fee_but_not_amount_or_currency(self):
        for text in ("Submission fees must be paid by card.",
                     "Our entry fee may be paid online using a debit card.",
                     "Reading fees can be paid via bank transfer.",
                     "The submission fee is payable with a credit card.",
                     "Please read the rules. Entry fees are payable online.",
                     "응모료는 반드시 납부해야 합니다.",
                     "접수비를 계좌 이체로 납부해야 한다.",
                     "심사료는 온라인 결제 필수"):
            with self.subTest(text=text):
                e, = fees(text)
                self.assertEqual(e.value_state, "stated")
                self.assertEqual(e.value, {"amount_min": None, "amount_max": None,
                                           "currency": None, "unit": None, "comparable": False})
                self.assertIn(e.excerpt, text)
                # The existing evidence verifier must reproduce the same value.
                self.assertTrue(promote.reparse_confirms({"excerpt": e.excerpt, "field": e.field,
                    "value_state": e.value_state, "value_json": json.dumps(e.value)}, False))

    def test_ambiguous_and_unrelated_payment_instructions(self):
        for text in ("Submission fees may apply.", "Fees must be paid online.",
                     "Membership fees must be paid online. We pay $50 per poem.",
                     "Workshop fees must be paid online. First prize is $900.",
                     "For our workshop, entry fees must be paid by card.",
                     "Other journals say: submission fees must be paid online.",
                     "Entry fees may be paid online if you select a manuscript appraisal.",
                     "Entry fees must be paid online for workshop registration.",
                     "Submission fees may be paid by card for members only.",
                     "Entry fees do not have to be paid online.",
                     "There is no charge for membership.",
                     "We pay $50 per poem. First prize is $900.",
                     "응모료는 경우에 따라 납부해야 합니다.",
                     "회원 가입비는 납부해야 합니다.",
                     "수강료는 온라인 결제 필수", "응모료는 납부할 수도 있습니다."):
            with self.subTest(text=text):
                self.assertEqual(fees(text), [])

    def test_heading_and_page_scope_reject_other_programs(self):
        for kw in ({"heading": "Poetry workshop"}, {"title": "Membership guidelines"},
                   {"title": "Tuition and fees"}, {"heading": "다른 강좌 안내"}):
            self.assertEqual(fees("Entry fees must be paid online.", **kw), [])

    def test_explicit_none_and_numeric_fee_keep_existing_values(self):
        for text in ("No entry fee.", "There is no reading fee.", "응모료는 없음"):
            self.assertEqual(fees(text)[0].value_state, "explicitly_none")
        for text, amount, currency in (("Entry fee: £12.", 12, "GBP"),
                                       ("응모료: 10,000원", 10000, "KRW")):
            e, = fees(text)
            self.assertEqual((e.value["amount_min"], e.value["currency"], e.value["comparable"]),
                             (amount, currency, True))

    def test_promote_supersedes_same_source_absence(self):
        svc = service()
        self.addCleanup(svc.close)
        outlet = add_outlet(svc)
        sid = add_source(svc, outlet=outlet)
        text = "Submission fees must be paid by card."
        doc = fee_doc(text)
        rev = store.ensure_revision(svc, sid, "synthetic-content", [], NOW, prof.EXTRACTOR_VERSION)
        raw = store.insert_raw(svc, NOW, source_id=sid, url="https://example.org/submit", kind="html",
                               fetch_method="html", outcome="ok", change_state="new",
                               text_hash="synthetic-content", fetched_at=NOW)
        gaps._record_absence(svc, "outlet", outlet, "fee.submission",
                             {"source_id": sid, "text_hash": "synthetic-content"}, NOW)
        old = svc.execute("SELECT fact_id FROM pb_fact").fetchone()[0]
        exts = [e for e in prof.extract(doc) if e.field == "fee.submission"]
        store.put_extractions(svc, raw, exts, "rules", prof.EXTRACTOR_VERSION, NOW)
        store.put_labels(svc, raw, prof.label_doc(doc, exts, dt.date.fromisoformat(NOW[:10])),
                         prof.LABELER_VERSION, NOW)
        result = promote.promote_raw(svc, raw, NOW)
        self.assertEqual((result.facts, result.superseded, result.calls_created), (1, 1, 0))
        row = svc.execute("SELECT f.*,e.excerpt FROM pb_fact f JOIN pb_evidence e USING(evidence_id) "
                          "WHERE f.superseded_at IS NULL").fetchone()
        self.assertEqual((row["predicate"], row["value_state"], row["epistemic"]),
                         ("fee.submission", "stated", "verified_fact"))
        self.assertEqual(row["excerpt"], text)
        self.assertEqual(row["inspected_revision_id"], rev)
        self.assertEqual(svc.execute("SELECT superseded_by FROM pb_fact WHERE fact_id=?", (old,)).fetchone()[0], row["fact_id"])
        self.assertEqual(promote.promote_raw(svc, raw, NOW).facts, 0)


if __name__ == "__main__":
    unittest.main()
