"""Non-writing notices on arts-council boards (venue rental, dance residency, hiring, admissions) are not writing calls."""
import datetime as dt
import unittest

from pbdata.collect.parse import Doc, Segment, parse_html
from pbdata.profiles import pitchboard as prof

TODAY = dt.date(2026, 10, 30)


def closes(text):
    return [e.value["date"] for e in prof.extract(Doc("html", segments=[Segment(0, "p", text)])) if e.field == "deadline.closes_on"]


def labels(title, body):
    doc = parse_html(("<html lang='ko'><head><title>%s</title></head><body><p>%s</p></body></html>" % (title, body)).encode(), "https://example.org/x")
    exts = prof.extract(doc)
    return {l.label: l for l in prof.label_doc(doc, exts, TODAY)}, exts


class NonWritingGateTests(unittest.TestCase):
    def test_dance_residency_board_row_has_no_call_deadline(self):
        row = ("진행중 NEW [해외레지던시참가지원] 2027년 오스트리아 댄스웹(danceWEB) 레지던시 참가 신청안내 · 지원신청기간 : "
               "2026. 10. 21 2026.10.21 ~ 2026.12.04 상세보기")
        self.assertEqual(closes(row), [])

    def test_impulstanz_row_has_no_call_deadline(self):
        row = ("진행중 NEW [해외레지던시참가지원] 2027년 오스트리아 임펄스탄츠 아틀라스 레지던시 참가 신청안내 · 임펄스탄츠 아틀라스 "
               "홈페이지를 통해 직접 신청(영문) - 지원신청기간 : 2026. 10... 2026.10.21 ~ 2026.12.04 상세보기")
        self.assertEqual(closes(row), [])
        self.assertEqual(closes("ImPulsTanz atlas residency, application period 2026.10.21 ~ 2026.12.04"), [])

    def test_literary_board_row_keeps_deadline(self):
        self.assertEqual(closes("진행중 [문학] 2027 문학 레지던시 참가 신청안내 · 신청기간 : 2026.10.21 ~ 2026.12.04"), ["2026-12-04"])

    def test_playwriting_overrides_theatre(self):
        self.assertEqual(closes("2027 연극 희곡 공모 접수기간 2026. 11. 1. ~ 2026. 11. 30."), ["2026-11-30"])

    def test_criticism_overrides_visual_art(self):
        self.assertEqual(closes("미술 비평 원고 모집 마감: 2026년 12월 1일"), ["2026-12-01"])

    def test_admissions_deadline_is_gated_even_with_translation_word(self):
        self.assertEqual(closes("번역아카데미 특별전형 합격자 원본 서류 제출 기한 : 2027. 1. 8.(금) 17:00"), [])

    def test_plain_korean_call_unchanged(self):
        self.assertEqual(closes("신청 기간 : 2026년 10월 8일(목) ~ 11월 5일(목) 15:00까지"), ["2026-11-05"])

    def test_english_call_unchanged(self):
        self.assertEqual(closes("Deadline: December 1, 2026"), ["2026-12-01"])

    def test_venue_rental_title_is_not_writing_call(self):
        labs, _ = labels("아르코·대학로예술극장 > 대관 공고", "2027년 정기대관 공모 접수기간: 2026. 11. 2. ~ 2026. 12. 4. 신청서 제출")
        self.assertEqual(labs["recruitment"].status, "not_writing_call")
        self.assertFalse(labs["recruitment"].open_call_eligible)

    def test_literary_call_title_still_eligible(self):
        labs, _ = labels("2027 신춘 문예 원고 공모", "응모 마감: 2026년 12월 4일 원고 접수")
        self.assertEqual(labs["recruitment"].status, "current")
        self.assertTrue(labs["recruitment"].open_call_eligible)

    def test_english_theatre_word_not_overridden_without_writing_cue(self):
        self.assertTrue(prof.non_writing("Theatre venue hire applications close 4 December 2026"))
        self.assertFalse(prof.non_writing("Call for theatre reviews: submissions close 4 December 2026"))
        self.assertFalse(prof.non_writing("A tender poem about rental flats"))   # 'tender'/'rental' alone are not notice types


class HeadingGateTests(unittest.TestCase):
    """Board detail pages put the notice title in a heading and a bare '마감일' (posting end) in a list item."""
    def _closes(self, heading, text):
        return [e.value["date"] for e in prof.extract(Doc("html", segments=[Segment(0, "li", text, heading)])) if e.field == "deadline.closes_on"]

    def test_event_notice(self):
        self.assertEqual(self._closes("2026년 <예술로 소풍-가을> 대학로 행사 안내 (10.31.(토))", "마감일 2026.10.31"), [])

    def test_forum_notice(self):
        self.assertEqual(self._closes("제4회 문화체육관광 디지털혁신 포럼(11.17(화))", "마감일 2026.11.17"), [])

    def test_hiring_interview_notice(self):
        self.assertEqual(self._closes("1) 서류전형 합격자 필수 제출자료", "제출기한 | 2026. 11. 4(수) 16:00까지"), [])

    def test_writing_call_heading_kept(self):
        self.assertEqual(self._closes("2027 문장웹진 원고 모집 공고", "마감일 2026.11.17"), ["2026-11-17"])

    def test_criticism_forum_call_kept(self):
        self.assertEqual(self._closes("2026 비평 포럼 발표 원고 공모", "마감일 2026.11.17"), ["2026-11-17"])


class ScholarshipGateTests(unittest.TestCase):
    def test_entrance_awards_title_gates_separate_application_timeline(self):
        labs, exts = labels("Entrance awards and scholarships | Douglas College",
                            "Eligibility guidelines. Application period: October 1, 2026 through March 31, 2027. "
                            "Award winners will be notified later.")
        self.assertFalse(any(e.field.startswith("deadline.") for e in exts))
        self.assertIn("rules", labs)
        self.assertIn("results", labs)
        self.assertEqual(labs["recruitment"].status, "not_writing_call")
        self.assertFalse(labs["recruitment"].open_call_eligible)

    def test_finance_deadline_segments_are_gated_without_a_title(self):
        for topic in ("Scholarships", "Student bursaries", "Entrance awards", "Financial aid", "Tuition", "Fees-and-finances"):
            with self.subTest(topic=topic):
                self.assertEqual(closes(topic + " application deadline: March 31, 2027."), [])

    def test_writing_scholarships_and_awards_keep_deadlines(self):
        for title in ("Poetry scholarship", "Writers scholarship", "Fiction award", "Essay contest"):
            with self.subTest(title=title):
                labs, exts = labels(title, "Submit your original poems or fiction manuscript. "
                                    "Scholarship application deadline: December 4, 2026.")
                self.assertEqual([e.value["date"] for e in exts if e.field == "deadline.closes_on"], ["2026-12-04"])
                self.assertTrue(labs["recruitment"].open_call_eligible)

    def test_writing_cues_in_another_paragraph_override_finance_title_and_deadline(self):
        doc = Doc("html", title="Scholarship award guidelines", segments=[
            Segment(0, "p", "Call for submissions: send an original poetry manuscript."),
            Segment(1, "p", "Scholarship application deadline: December 4, 2026.")])
        exts = prof.extract(doc)
        self.assertEqual([e.value["date"] for e in exts if e.field == "deadline.closes_on"], ["2026-12-04"])
        self.assertTrue(any(l.open_call_eligible for l in prof.label_doc(doc, exts, TODAY)))

    def test_literary_title_overrides_finance_in_a_bare_deadline_segment(self):
        labs, exts = labels("Poetry scholarship guidelines", "Scholarship application deadline: December 4, 2026.")
        self.assertEqual([e.value["date"] for e in exts if e.field == "deadline.closes_on"], ["2026-12-04"])
        self.assertTrue(labs["recruitment"].open_call_eligible)

    def test_og_title_gates_bare_deadline(self):
        doc = Doc("html", meta={"og:title": "Student bursary guidelines"},
                  segments=[Segment(0, "p", "Eligibility rules. Deadline: December 4, 2026.")])
        self.assertFalse(any(e.field == "deadline.closes_on" for e in prof.extract(doc)))
        self.assertEqual(next(l for l in prof.label_doc(doc, [], TODAY) if l.label == "recruitment").status,
                         "not_writing_call")

    def test_unrelated_writing_navigation_does_not_override_finance_title(self):
        doc = Doc("html", title="Entrance awards and scholarships", segments=[
            Segment(0, "p", "Creative Writing courses | Program Reviews | Child and Youth Care essay form"),
            Segment(1, "p", "Eligibility guidelines. Application period: October 1, 2026 through March 31, 2027."),
            Segment(2, "p", "Award winners will be notified later.")])
        exts = prof.extract(doc)
        self.assertFalse(any(e.field == "deadline.closes_on" for e in exts))
        self.assertEqual(next(l for l in prof.label_doc(doc, exts, TODAY) if l.label == "recruitment").status,
                         "not_writing_call")


class BoardRowTitleGateTests(unittest.TestCase):
    """Card/board lists (childbook.org 교육 프로그램): the row title sits a few lines above a bare '접수 : a~b' line."""
    def _closes(self, lines):
        segs = [Segment(i, k, t) for i, (k, t) in enumerate(lines)]
        return [e.value["date"] for e in prof.extract(Doc("html", segments=segs)) if e.field == "deadline.closes_on"]

    def _row(self, title, period, target="일반", status="접수중"):
        return [("p", title), ("p", "신청 : 0/25"), ("li", "주관 : 중앙 | 일시 : 2026-11-27 ~ 2026-11-27 | 장소 : ZOOM"),
                ("li", "접수 : %s | 대상 : %s" % (period, target)), ("p", status)]

    def test_membership_drive_row_has_no_call_deadline(self):
        self.assertEqual(self._closes(self._row("회원모집 2026년 신입 회원을 모집합니다", "2026-01-01~2026-12-31")), [])

    def test_book_discussion_signup_row_has_no_call_deadline(self):
        self.assertEqual(self._closes(self._row("토론회 목록위원회 책토론회 -예술팀-", "2026-10-27~2026-11-26", "회원")), [])

    def test_writing_contest_row_keeps_deadline(self):
        self.assertEqual(self._closes(self._row("공모 2026 어린이 동화 원고 공모", "2026-10-27~2026-11-26")), ["2026-11-26"])

    def test_previous_row_title_does_not_leak(self):
        # the membership row's title must not gate the next (writing) row: the walk stops at the previous row's status word
        lines = self._row("회원모집 2026년 신입 회원을 모집합니다", "2026-01-01~2026-12-31") + [("li", "접수 : 2026-11-01~2026-11-30 | 대상 : 일반")]
        self.assertEqual(self._closes(lines), ["2026-11-30"])

    def test_prose_paragraph_above_is_not_row_context(self):
        long = "본 공모는 " + "신입 회원 여부와 관계없이 누구나 응모할 수 있습니다. " * 4
        self.assertEqual(self._closes([("p", long), ("li", "원고 마감 : 2026-11-30")]), ["2026-11-30"])

    def test_signup_on_site_is_not_a_membership_drive(self):
        self.assertFalse(prof.non_writing("홈페이지 회원가입 후 응모, 접수 기간 2026.10.19 ~ 2026.12.13"))

    def test_policy_forum_heading_gated(self):
        doc = Doc("html", segments=[Segment(0, "li", "마감일 2026.11.28", "문화예술진흥법 개정을 위한 정책 토론회(11.28(수))")])
        self.assertEqual([e for e in prof.extract(doc) if e.field == "deadline.closes_on"], [])

    def test_labeler_no_longer_marks_membership_board_as_current_call(self):
        rows = "".join("<p>%s</p><p>신청 : 0/0</p><ul><li>주관 : 중앙 | 일시 : 2026-03-01 ~ 2026-12-31</li><li>접수 : %s | 대상 : 일반</li></ul><p>접수중</p>" % r
                       for r in [("회원모집 2026년 신입 회원을 모집합니다", "2026-01-01~2026-12-31")])
        doc = parse_html(("<html lang='ko'><head><title>어린이도서연구회</title></head><body>%s</body></html>" % rows).encode(), "https://example.org/edu/program_list.html")
        exts = prof.extract(doc)
        rec = [l for l in prof.label_doc(doc, exts, TODAY) if l.label == "recruitment"]
        self.assertFalse(any(l.open_call_eligible for l in rec))


class PromoteSkipsNonWritingTests(unittest.TestCase):
    def test_status_in_skip_set(self):
        from pbdata.collect import promote
        self.assertIn("not_writing_call", promote.SKIP_LABEL_STATUS)

    def test_rules_only_nonwriting_title_cannot_promote_even_with_a_future_deadline(self):
        from helpers import service, add_source, add_outlet, add_revision
        from pbdata.collect import promote, store

        # Retain a future extraction to exercise the promotion gate independently of deadline filtering.
        # Results in the body suppress the old deadline-based recruitment fallback: the rules label alone
        # previously admitted this page to promotion.
        body = "Eligibility rules. Application deadline: March 31, 2027. Award winners will be notified later."
        for title, writing in (("Entrance awards and scholarships", False), ("회원모집", False),
                               ("Venue rental guidelines", False), ("Poetry scholarship guidelines", True)):
            with self.subTest(title=title), service() as svc:
                self.addCleanup(svc.close)
                now = "2026-11-06T00:00:00Z"
                doc = Doc("html", title=title, segments=[Segment(0, "p", body)])
                exts = prof.extract(Doc("html", segments=doc.segments))
                self.assertTrue(any(e.field == "deadline.closes_on" for e in exts))
                labs = prof.label_doc(doc, exts, dt.date(2026, 11, 6))
                self.assertTrue(any(l.label == "rules" for l in labs))
                if writing:
                    self.assertFalse(any(l.label == "recruitment" for l in labs))
                else:
                    self.assertEqual(next(l for l in labs if l.label == "recruitment").status, "not_writing_call")
                sid = add_source(svc, outlet=add_outlet(svc))
                add_revision(svc, sid, fetched_at=now)
                rid = store.insert_raw(svc, now, source_id=sid, url="https://example.org/x", kind="html",
                                       fetch_method="html", outcome="ok", change_state="new", text_hash="h1", fetched_at=now)
                store.put_extractions(svc, rid, exts, "rules", prof.EXTRACTOR_VERSION, now)
                store.put_labels(svc, rid, labs, prof.LABELER_VERSION, now)
                stats = promote.promote_raw(svc, rid, now)
                self.assertEqual(stats.calls_created, int(writing))
                self.assertEqual(svc.execute("SELECT count(*) FROM pb_call_edition").fetchone()[0], int(writing))


if __name__ == "__main__":
    unittest.main()
