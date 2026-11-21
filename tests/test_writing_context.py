"""An open call needs writing context: university/association/HR pages with a deadline and 'apply'/'now open' cues are not writing calls."""
import datetime as dt
import unittest

from pbdata.collect.parse import Doc, Segment, parse_html
from pbdata.profiles import pitchboard as prof

TODAY = dt.date(2026, 11, 22)


def rec(title, paras, url="https://example.org/x"):
    body = "".join("<p>%s</p>" % p for p in paras)
    doc = parse_html(("<html><head><title>%s</title></head><body>%s</body></html>" % (title, body)).encode(), url)
    labs = [l for l in prof.label_doc(doc, prof.extract(doc), TODAY) if l.label == "recruitment"]
    return labs[0] if labs else None


class WritingContextTests(unittest.TestCase):
    def test_association_book_nomination_without_writing_context_is_not_a_call(self):
        l = rec("Gladys M. Kammerer Award - American Political Science Association",
                ["The award honors the best political science publication in the field of U.S. national policy.",
                 "Nominations close February 10, 2027. Nomination applications are accepted through the online form."])
        self.assertEqual(l.status, "not_writing_call")
        self.assertFalse(l.open_call_eligible)

    def test_university_news_now_open_is_not_a_call(self):
        l = rec("Care Closet Opens Allowing Students Easy Access | Kent State",
                ["The care closet is now open to all students. Apply for assistance at the front desk."])
        self.assertFalse(l.open_call_eligible)
        self.assertEqual(l.status, "not_writing_call")

    def test_graduate_dates_deadline_with_submission_cue_is_not_a_call(self):
        l = rec("Important Graduate Dates | Office of Student Services",
                ["Final deadline for all dissertation corrections: December 4, 2026. Submit your application for graduation."])
        self.assertFalse(l.open_call_eligible)

    def test_job_posting_is_not_a_call(self):
        l = rec("Open Positions at the Joint Committee | Department of Economics",
                ["To apply: please submit a cover letter and resume by December 4, 2026. Applications are reviewed on a rolling basis."])
        self.assertFalse(l.open_call_eligible)

    def test_literary_title_keeps_call(self):
        l = rec("Poetry Awards - Writer's Digest", ["Deadline: December 1, 2026. Submit your entry online."])
        self.assertTrue(l.open_call_eligible)
        self.assertEqual(l.status, "current")

    def test_publishing_title_without_literary_word_keeps_call(self):
        l = rec("Submissions Open for the 2027 Publishing Triangle Awards",
                ["Submissions open September 21, 2026, and close December 4, 2026."])
        self.assertTrue(l.open_call_eligible)

    def test_call_cue_in_title_keeps_call(self):
        l = rec("Annual Prize call for entries", ["Entries close on December 20, 2026."])
        self.assertTrue(l.open_call_eligible)

    def test_call_sentence_naming_writing_keeps_call(self):
        l = rec("공지", ["2026 시 공모 접수기간 : 2026-11-01 ~ 2026-12-31 원고 응모"])
        self.assertTrue(l.open_call_eligible)

    def test_literary_body_keeps_call_with_generic_title(self):
        para = "We publish fiction, poetry and essays by emerging writers. Our editors read every manuscript."
        l = rec("Home", [para, para, para, "Deadline: December 15, 2026. Apply via the online form."])
        self.assertTrue(l.open_call_eligible)

    def test_segment_heading_counts_as_context(self):
        doc = Doc("html", segments=[Segment(0, "p", "Deadline: December 15, 2026. Apply online.", "Poetry contest")])
        self.assertTrue(prof.has_writing_context(doc))
        self.assertFalse(prof.has_writing_context(Doc("html", segments=[Segment(0, "p", "Deadline: December 15, 2026. Apply online.")])))


if __name__ == "__main__":
    unittest.main()
