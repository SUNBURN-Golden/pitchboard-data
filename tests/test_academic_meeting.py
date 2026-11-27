"""Academic meetings are not writing calls (2026-11-27): the APSA 2027 Annual Meeting call for proposals stayed an open call
because "call for proposals" / "submission" gave it writing context. A literary/writers' conference keeps its writing cue."""
import datetime as dt
import unittest

from pbdata.collect.parse import parse_html
from pbdata.profiles import pitchboard as prof

TODAY = dt.date(2026, 11, 27)


def rec(title, heads, paras, url="https://example.org/x"):
    body = "".join("<h2>%s</h2>" % h for h in heads) + "".join("<p>%s</p>" % p for p in paras)
    doc = parse_html(("<html><head><title>%s</title></head><body>%s</body></html>" % (title, body)).encode(), url)
    exts = prof.extract(doc)
    labs = [l for l in prof.label_doc(doc, exts, TODAY) if l.label == "recruitment"]
    return (labs[0] if labs else None), sorted({e.value["date"] for e in exts if e.field == "deadline.closes_on"})


class AcademicMeetingTests(unittest.TestCase):
    def test_political_science_annual_meeting_cfp_is_not_a_call(self):
        l, closes = rec("APSA Annual Meeting 2027", ["Now Open! 2027 APSA Call for Proposals 123rd Annual Meeting & Exhibition", "Submission Options"],
                        ["The 2027 APSA Annual Meeting proposal submission system is now open! Submission Deadline: January 13, 2027, at 11:59 p.m. Pacific.",
                         "When submitting and accepting panel and individual paper proposals, please follow the participation rules."],
                        "https://connect.apsanet.org/apsa2027")
        self.assertEqual("not_writing_call", l.status)
        self.assertFalse(l.open_call_eligible)
        self.assertEqual([], closes)

    def test_isa_convention_and_symposium_are_not_calls(self):
        for title in ("ISA Annual Convention 2027", "Guidelines for Symposium Proposals - PS: Political Science", "2027 학술대회 발표 신청"):
            l, _ = rec(title, ["Call for Proposals"], ["Proposals are now open. Deadline: December 15, 2026."])
            self.assertFalse(l.open_call_eligible, title)

    def test_writers_conference_and_literary_journal_cfp_stay_calls(self):
        l, closes = rec("Writers' Conference Scholarship Contest", ["Call for Submissions"],
                        ["Submit up to 10 pages of fiction or poetry. Submissions close December 15, 2026."])
        self.assertTrue(l.open_call_eligible)
        self.assertEqual(["2026-12-15"], closes)
        l, _ = rec("Tulsa Studies in Women's Literature: Call for Papers", ["Call for Papers"],
                   ["Essays on women's literature are due January 31, 2027."])
        self.assertTrue(l.open_call_eligible)


if __name__ == "__main__":
    unittest.main()
