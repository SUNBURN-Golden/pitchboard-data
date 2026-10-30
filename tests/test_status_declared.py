"""status.declared must describe the call itself: Korean penalty/withdrawal clauses and schedule labels are not declarations."""
import unittest

from pbdata.collect.parse import Doc, Segment
from pbdata.profiles import pitchboard as prof


def declared(text):
    return [e.value["declared"] for e in prof.extract(Doc("html", segments=[Segment(0, "p", text)])) if e.field == "status.declared"]


class StatusDeclaredTests(unittest.TestCase):
    # false cancellations seen on live pages (weinovel contest wrongly moved to cancelled on 2026-10-31)
    def test_award_revocation_clause_is_not_cancellation(self):
        self.assertEqual(declared("권리 문제가 확인되면 접수 단계에서는 실격, 수상 뒤에는 수상 취소와 상금 환수가 따릅니다."), [])

    def test_entry_withdrawal_is_not_cancellation(self):
        self.assertEqual(declared("참가 취소는 운영자 확인 후 처리되며, 취소한 참가작도 1인 2편에 포함됩니다."), [])

    def test_selection_revocation_and_ui_text_is_not_cancellation(self):
        for t in ("아래의 경우 선정 취소 및 지원금이 회수될 수 있습니다.", "ㅇ 선정취소 및 지원금 회수", "저장 취소", "뉴스레터 신청/취소",
                  "참석이 어려운 경우 반드시 사전에 예약 취소 연락을 부탁드립니다.", "희망자가 없을 시 지원은 취소될 수 있음"):
            self.assertEqual(declared(t), [], t)

    def test_english_incidental_cancel_is_not_cancellation(self):
        self.assertEqual(declared("Many firms have cancelled orders from Dhaka."), [])

    def test_real_cancellations_still_detected(self):
        for t in ("2026 신춘문예 공모가 취소되었습니다.", "올해 공모전은 전면 취소합니다.", "모집 취소 안내",
                  "The 2026 contest has been cancelled.", "We are no longer accepting submissions."):
            self.assertIn(declared(t)[:1], (["cancelled"], ["closed"]), t)
        self.assertEqual(declared("2026 신춘문예 공모가 취소되었습니다."), ["cancelled"])

    # schedule labels are not "closed" declarations
    def test_closing_labels_are_not_closed(self):
        for t in ("접수 마감", "모집 마감 : 2026년 10월 5일(월) 13:00", "접수 마감일 | ~ 4월 30일", "9월 21일 월요일부터 모집마감시까지 선착순",
                  "※ 접수 마감일 이전 발간 완료된 자료는 지원 불가", "접수 종료일: 11월 3일"):
            self.assertEqual(declared(t), [], t)

    def test_real_closings_still_detected(self):
        for t in ("올해 원고 접수가 마감되었습니다.", "모집이 마감되었습니다. 감사합니다.", "접수 마감합니다", "본 공모는 접수 종료되었습니다.",
                  "Submissions are now closed!"):
            self.assertEqual(declared(t), ["closed"], t)


if __name__ == "__main__":
    unittest.main()
