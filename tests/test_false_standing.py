"""False standing calls (2026-11-27): a Writer's Digest blog post ("I'm always open to other takes"), Narrative's graphic-art
guidelines and a university graduate-admissions page ("reviewed on a rolling basis") became standing calls."""
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
    return labs[0] if labs else None


class StandingPhraseTests(unittest.TestCase):
    def test_conversational_always_open_is_not_standing(self):
        self.assertIsNone(prof.STANDING_RE.search("I'm always open to other takes on this poetry form."))
        self.assertIsNone(prof.STANDING_RE.search("Our door is always open if you have questions."))

    def test_submission_always_open_is_standing(self):
        for s in ("Submissions are always open.", "We are always open to new work.", "The magazine is always open for submissions.",
                  "We read on a rolling basis.", "상시 접수합니다."):
            self.assertIsNotNone(prof.STANDING_RE.search(s), s)


class NonWritingPageTests(unittest.TestCase):
    def test_graduate_admissions_is_not_a_call(self):
        l = rec("Admission Deadlines & Requirements | The University of Tulsa", ["Graduate Admission"],
                ["Applications for creative writing and English graduate programs are reviewed on a rolling basis. Submit your writing sample."],
                "https://utulsa.edu/admissions/graduate-admission/admission-deadline-requirements/")
        self.assertTrue(l is None or not l.open_call_eligible)

    def test_graphic_art_guidelines_are_not_a_call(self):
        l = rec("Graphic Art Guidelines | Narrative Magazine", ["Graphic Art Submissions"],
                ["Narrative accepts submissions of graphic arts and photography year-round."],
                "https://www.narrativemagazine.com/graphic-art-guidelines")
        self.assertTrue(l is None or not l.open_call_eligible)

    def test_writing_guidelines_with_photography_mention_stay_calls(self):
        l = rec("Submission Guidelines | Literary Journal", ["Submissions"],
                ["We accept fiction, poetry and nonfiction year-round. Submissions are always open. We do not accept photography."])
        self.assertIsNotNone(l)
        self.assertTrue(l.open_call_eligible)


if __name__ == "__main__":
    unittest.main()
