import unittest
from unittest.mock import patch

from pbdata.daemon import questions


def projection(fee_answer="stated", *, stale=False, applicable_now="yes"):
    return {
        "call_id": "test-call",
        "owner": {"name": "Test owner"},
        "status": "open",
        "closes_on": "2026-12-01",
        "applicable_now": applicable_now,
        "status_history": [],
        "conditions": {
            "deadline.closes_on": {
                "answer": "stated",
                "facts": [{"epistemic": "verified_fact", "stale": False,
                           "evidence": "deadline-evidence"}],
            },
            "fee.submission": {
                "answer": fee_answer,
                "facts": [] if fee_answer == "unknown" else [{
                    "epistemic": "verified_fact", "stale": stale,
                    "value": None if fee_answer == "explicitly_none" else {"amount": 10},
                    "evidence": "fee-evidence",
                }],
            },
            "eligibility.region": {"answer": "unknown", "facts": []},
        },
    }


class Q1BucketTests(unittest.TestCase):
    def q1_bucket(self, p):
        with patch.object(questions, "_rows", return_value=[p]):
            return questions.answer_all(None)["Q1_open_now"][0]["bucket"]

    def test_unknown_fee_is_possible(self):
        self.assertEqual(self.q1_bucket(projection("unknown")), "적합 가능(미확인 있음)")

    def test_explicitly_none_verified_fresh_fee_is_confirmed(self):
        self.assertEqual(self.q1_bucket(projection("explicitly_none")), "확정 적합")

    def test_stated_verified_fresh_fee_is_confirmed(self):
        self.assertEqual(self.q1_bucket(projection()), "확정 적합")

    def test_stale_fee_is_possible(self):
        self.assertEqual(self.q1_bucket(projection(stale=True)), "적합 가능(미확인 있음)")

    def test_conflicting_fee_needs_review(self):
        self.assertEqual(self.q1_bucket(projection("conflict")), "검수 필요(충돌)")

    def test_not_applicable_is_closed(self):
        p = projection(applicable_now="no")
        self.assertEqual(questions.bucket(p, questions.Q1_REQUIRED), "부적합/마감")
        with patch.object(questions, "_rows", return_value=[p]):
            self.assertEqual(questions.answer_all(None)["Q1_open_now"], [])


if __name__ == "__main__":
    unittest.main()
