"""Board post metadata (2026-11-27): ARKO view pages print "시작일 / 마감일 / 조회수 / 등록일 / 담당부서" for the post's display
period. That 마감일 is not an application deadline; a deadline stated in the body still is."""
import datetime as dt
import unittest

from pbdata.collect.parse import parse_html
from pbdata.profiles import pitchboard as prof

TODAY = dt.date(2026, 11, 27)


def page(title, meta, body):
    html = "<html><head><title>한국문화예술위원회</title></head><body><h2>%s</h2><ul>%s</ul>%s</body></html>" % (
        title, "".join("<li>%s</li>" % m for m in meta), "".join("<p>%s</p>" % b for b in body))
    doc = parse_html(html.encode(), "https://www.arko.or.kr/board/view/4053?cid=1")
    exts = prof.extract(doc)
    labs = [l for l in prof.label_doc(doc, exts, TODAY) if l.label == "recruitment"]
    return sorted({e.value["date"] for e in exts if e.field == "deadline.closes_on"}), (labs[0] if labs else None)

META = ["시작일 2026.11.30", "마감일 2026.12.10", "조회수 976", "등록일 2026.11.26", "담당부서 문학팀", "담당자 홍길동"]


class BoardPostingPeriodTests(unittest.TestCase):
    def test_metadata_end_date_is_not_a_deadline(self):
        closes, lab = page("2027 문학 창작 지원 공모 안내", META, ["문학 작가를 위한 창작 지원 공모입니다. 원고를 제출해 주세요."])
        self.assertEqual([], closes)
        self.assertFalse(lab.open_call_eligible)

    def test_body_deadline_still_extracted(self):
        closes, lab = page("2027 문학 창작 지원 공모 안내", META, ["문학 작가를 위한 창작 지원 공모입니다.", "원고 접수 마감 : 2026년 12월 5일(토) 18:00까지"])
        self.assertEqual(["2026-12-05"], closes)
        self.assertTrue(lab.open_call_eligible)

    def test_bare_deadline_line_without_board_metadata_is_kept(self):
        closes, _ = page("2027 시 공모", [], ["시 원고를 모집합니다.", "마감일 2026.12.10"])
        self.assertEqual(["2026-12-10"], closes)

    def test_explicit_posting_period_line_is_never_a_deadline(self):
        closes, _ = page("2027 시 공모", [], ["시 원고를 모집합니다.", "게시기간 2026.11.01 ~ 2026.12.10"])
        self.assertEqual([], closes)


if __name__ == "__main__":
    unittest.main()
