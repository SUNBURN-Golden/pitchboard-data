import unittest

from pbdata.collect.shadow import common, planner, runner, schema
from pbdata.collect.shadow.review import reviews
from pbdata.collect.parse import Doc, Segment
from test_llm_extract import URL, page, proposal
from test_llm_improve import candidate, export


def check(state, quote, text=None, doc_text=None):
    raw = dict.fromkeys(schema.FIELDS)
    raw["simultaneous"] = dict(value=dict(text=text or quote, state=state), quote=quote)
    doc = Doc("html", segments=[Segment(0, "p", doc_text or quote)])
    fields, reasons = schema.validate(raw, doc, "2026-11-05")
    return fields["simultaneous"], reasons.get("simultaneous")


class SimultaneousSchemaTests(unittest.TestCase):
    def test_version_and_fields(self):
        self.assertEqual(schema.VERSION, "pb-shadow-3")
        self.assertEqual(schema.FIELDS[-1], "simultaneous")
        self.assertNotIn("simultaneous", schema.FIELDS_V2)

    def test_english_accept(self):
        for state, q in (("allowed", "Simultaneous submissions are welcome."),
                         ("allowed", "We accept simultaneous submissions."),
                         ("not_allowed", "No simultaneous submissions."),
                         ("not_allowed", "Please do not send simultaneous submissions."),
                         ("not_allowed", "Simultaneous submissions are not accepted."),
                         ("not_allowed", "Work must be previously unpublished and not under consideration elsewhere.")):
            with self.subTest(q=q):
                got, reason = check(state, q)
                self.assertIsNotNone(got, reason)

    def test_korean_accept(self):
        for state, q in (("not_allowed", "타 공모전 중복 응모 불가"), ("not_allowed", "중복 투고는 금지됩니다."),
                         ("allowed", "동시 투고 가능"), ("allowed", "중복 응모 허용")):
            with self.subTest(q=q):
                got, reason = check(state, q)
                self.assertIsNotNone(got, reason)

    def test_missing_cue_or_wrong_polarity_rejected(self):
        for state, q in (("allowed", "Send your best stories by December 1."),
                         ("not_allowed", "Submissions are open until December 1, 2026."),
                         ("allowed", "No simultaneous submissions."),
                         ("allowed", "Simultaneous submissions are not accepted."),
                         ("not_allowed", "Simultaneous submissions are welcome."),
                         ("allowed", "Please read our simultaneous submissions policy."),
                         ("unknown", "Simultaneous submissions are welcome."),
                         ("allowed", "중복 투고 불가능"),
                         ("not_allowed", "동시 투고 가능"),
                         ("allowed", "원고를 보내 주세요")):
            with self.subTest(q=q):
                got, reason = check(state, q)
                self.assertIsNone(got)
                self.assertEqual(reason, "unsupported_simultaneous_state")

    def test_quote_must_be_verbatim_in_source(self):
        got, reason = check("allowed", "Simultaneous submissions are welcome.", doc_text="Submissions are open.")
        self.assertIsNone(got)
        self.assertEqual(reason, "quote_not_found")
        got, reason = check("allowed", "Simultaneous submissions are welcome.", text="Simultaneous submissions OK",
                            doc_text="Simultaneous submissions are welcome.")
        self.assertEqual(reason, "value_not_verbatim")

    def test_absence_is_null_not_allowed(self):
        fields, reasons = schema.validate(dict.fromkeys(schema.FIELDS), page().doc(), "2026-11-05")
        self.assertIsNone(fields["simultaneous"])
        self.assertNotIn("simultaneous", reasons)

    def test_old_schema_validator_has_no_simultaneous_key(self):
        raw = proposal()
        raw.pop("simultaneous")
        fields, reasons = schema.validator("pb-shadow-2")(raw, page().doc(), "2026-11-05")
        self.assertEqual(reasons, {})
        self.assertTrue(fields["pay"])
        self.assertNotIn("simultaneous", fields)
        _, reasons = schema.validator("pb-shadow-3")(raw, page().doc(), "2026-11-05")
        self.assertEqual(reasons, {"simultaneous": "missing_key"})


class SimultaneousPlannerTests(unittest.TestCase):
    def test_predicate_supported_lowest_priority_static(self):
        self.assertEqual(max(planner.PRIORITY.values()), planner.PRIORITY["rights.simultaneous"])
        self.assertIn("rights.simultaneous", planner.STATIC)
        plan = planner.plan(export(candidate(predicate="rights.simultaneous")))
        self.assertEqual([t["predicate"] for t in plan["targets"][0]["targets"]], ["rights.simultaneous"])

    def test_unsupported_predicates_still_rejected(self):
        plan = planner.plan(export(candidate(predicate="policy.ai")))
        self.assertEqual(plan["targets"], [])
        self.assertIn("unsupported_predicate", str(plan))


def record(state, rule_states):
    q = "Simultaneous submissions are welcome."
    return {"outcome": "ok", "schema": schema.VERSION, "attempt_id": "att_1", "requested_url": URL, "final_url": URL,
            "body_sha256": "0" * 64, "text_sha256": "1" * 64, "as_of": "2026-11-05",
            "baseline": {"extensions": [{"field": "rights.simultaneous", "value": {"state": s}, "value_state": "stated",
                                         "excerpt": "x"} for s in rule_states],
                         "labels": [], "deadline": [], "real_call": True, "current_open": True},
            "fields": {"simultaneous": {"value": {"text": q, "state": state}, "quote": q}}}


class SimultaneousCompareTests(unittest.TestCase):
    def test_conflict_goes_to_review(self):
        rs = reviews(record("allowed", ["not_allowed"]))
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0]["predicate"], "rights.simultaneous")
        self.assertEqual(rs[0]["reasons"], ["field_conflict"])

    def test_agreement_no_review_and_fill_is_candidate(self):
        self.assertEqual(reviews(record("allowed", ["allowed"])), [])
        rs = reviews(record("allowed", []))
        self.assertEqual(rs[0]["reasons"], ["candidate_fill"])

    def test_rules_disagreeing_among_themselves_is_ambiguous(self):
        rs = reviews(record("allowed", ["allowed", "not_allowed"]))
        self.assertEqual(rs[0]["reasons"], ["ambiguous_rules"])

    def test_old_rows_load_in_score(self):
        from pbdata.collect.shadow import compare
        raw = proposal()
        raw.pop("simultaneous")
        fields, _ = schema.validator("pb-shadow-2")(raw, page().doc(), "2026-11-05")
        item = compare.Item("fixture", URL, "2026-11-05", page(), page().body_sha256,
                            common.digest(page().doc().text()), "en", runner.baseline(page().doc(), "2026-11-05"))
        result = compare.score([item], {item.key: dict(schema="pb-shadow-2", fields=fields, text_sha256=item.text_hash)})
        cov = result["summaries"]["fixture"]["field_coverage"]
        self.assertEqual(cov["simultaneous"], 0)
        self.assertTrue(cov["pay"])
        v1 = {k: v for k, v in fields.items() if k != "submission_fee"}
        result = compare.score([item], {item.key: dict(schema="pb-shadow-1", fields=v1, text_sha256=item.text_hash)})
        self.assertEqual(result["summaries"]["fixture"]["field_coverage"]["simultaneous"], 0)


if __name__ == "__main__":
    unittest.main()
