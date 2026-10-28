"""Korean deadline phrasing: application-period labels, ranges whose start carries a 24h time, '18:00까지' times."""
import unittest

from pbdata.collect.dates import find_dates
from pbdata.collect.parse import Doc, Segment
from pbdata.profiles import pitchboard as prof


def deadlines(text):
    exts = prof.extract(Doc("html", segments=[Segment(0, "p", text)]))
    return {e.field: e.value for e in exts if e.field.startswith("deadline.")}


class KoreanDeadlineTests(unittest.TestCase):
    def test_application_period_label_with_range(self):
        d = deadlines("신청기간 | 2026. 9. 14.(월) ~ 10. 16.(금) 23:59:59 (한국 표준시간 기준)")
        self.assertEqual(d["deadline.opens_on"]["date"], "2026-09-14")
        self.assertEqual(d["deadline.closes_on"]["date"], "2026-10-16")
        self.assertEqual(d["deadline.closes_on"]["time"], "23:59")
        self.assertEqual(d["deadline.closes_on"]["tz_source"], "unknown")   # '한국 표준시간' is not guessed into an offset

    def test_jiwon_jeopsu_with_explicit_kst(self):
        d = deadlines("지원접수 | 2026. 9. 14.(월) ~ 10. 16.(금) 23:59:59 (KST)")
        self.assertEqual(d["deadline.closes_on"]["date"], "2026-10-16")
        self.assertEqual(d["deadline.closes_on"]["tz_source"], "explicit")
        self.assertEqual(d["deadline.closes_on"]["utc_offset_hours"], 9.0)

    def test_range_start_with_time_before_separator(self):
        hits = find_dates("◦ 입력 기간 : 2026. 8. 31.(월) 10:00 ~ 9. 18.(금) 17:00 (한국 시간)")
        self.assertEqual([h.iso for h in hits], ["2026-08-31", "2026-09-18"])
        self.assertEqual(hits[0].time, "10:00")
        self.assertEqual(hits[1].time, "17:00")
        self.assertFalse(hits[1].year_explicit)

    def test_bare_label_followed_by_date(self):
        d = deadlines("신청 2026. 9. 15.(화) ~ 9. 28.(월) 13:00 / QR코드 또는 신청 링크")
        self.assertEqual(d["deadline.closes_on"]["date"], "2026-09-28")
        self.assertEqual(d["deadline.closes_on"]["time"], "13:00")

    def test_kkaji_24h_time(self):
        d = deadlines("원고 마감: 2026년 11월 30일(월) 18:00까지 이메일 접수")
        self.assertEqual(d["deadline.closes_on"], {"date": "2026-11-30", "tz_source": "unknown", "time": "18:00"})

    def test_time_range_is_not_a_date_range(self):
        hits = find_dates("일시 2026. 9. 30.(수) 14:00–18:00")
        self.assertEqual([h.iso for h in hits], ["2026-09-30"])
        self.assertEqual(hits[0].time, "14:00")

    def test_not_deadlines(self):
        self.assertEqual(deadlines("신청 방법은 홈페이지 참조, 2026.10.01 게시"), {})
        self.assertEqual(deadlines("선정발표 : 2026년 10월 12일(월) 문자 및 이메일 개별 안내"), {})
        self.assertEqual(deadlines("지원 기간 석사 2년, 박사 3년"), {})
        self.assertEqual(deadlines("신청: ~ 9. 28.(월) 13:00까지"), {})          # no explicit year -> nothing

    def test_ratio_like_numbers_are_not_times(self):
        hits = find_dates("마감 2026.11.30 ① 해외출판사와 1:1 저작권 면담")
        self.assertIsNone(hits[0].time)

    def test_english_am_pm_unchanged(self):
        d = deadlines("Deadline: November 15, 2026, 11:59 p.m. EST")
        self.assertEqual(d["deadline.closes_on"]["time"], "23:59")
        self.assertEqual(d["deadline.closes_on"]["utc_offset_hours"], -5.0)

    def test_excerpt_reparse_reproduces_value(self):
        # the deterministic verifier re-runs the rules on the stored excerpt; the new cues must reproduce on it
        text = "지원접수 | 2026. 9. 14.(월) ~ 10. 16.(금) 23:59:59 (KST)"
        e = [x for x in prof.extract(Doc("html", segments=[Segment(0, "p", text)])) if x.field == "deadline.closes_on"][0]
        again = [x for x in prof.extract(Doc("html", segments=[Segment(0, "p", e.excerpt)])) if x.field == "deadline.closes_on"][0]
        self.assertEqual(e.value, again.value)


if __name__ == "__main__":
    unittest.main()
