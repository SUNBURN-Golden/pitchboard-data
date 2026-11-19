"""A live call deadline the same source no longer states is withdrawn on re-extraction (childbook.org membership drive)."""
import datetime as dt
import unittest

from pbdata.collect import promote, store
from pbdata.collect.parse import Doc, Segment
from pbdata.profiles import pitchboard as prof

from helpers import add_outlet, add_revision, add_source, service

CALL = "2026 시 공모 접수기간 : 2026-11-01 ~ 2026-12-31 원고 응모"
MEMBERSHIP = "회원모집 2026년 신입 회원을 모집합니다 접수 : 2026-01-01~2026-12-31 | 대상 : 일반 접수중"
OTHER = "2026 시 공모 접수기간 : 2026-11-01 ~ 2027-01-15 원고 응모"


def _raw(svc, sid, body, now, h):
    doc = Doc("html", title="공모", segments=[Segment(0, "p", body)])
    exts = prof.extract(doc)
    labs = prof.label_doc(doc, exts, dt.date.fromisoformat(now[:10]))
    add_revision(svc, sid, content_hash=h, fetched_at=now)
    rid = store.insert_raw(svc, now, source_id=sid, url="https://example.org/x", kind="html", fetch_method="html",
                           outcome="ok", change_state="changed", text_hash=h, fetched_at=now)
    store.put_extractions(svc, rid, exts, "rules", prof.EXTRACTOR_VERSION, now)
    store.put_labels(svc, rid, labs, prof.LABELER_VERSION, now)
    return rid


def live_dates(svc, sid=None):
    q = ("SELECT json_extract(value_json,'$.date') FROM pb_fact WHERE predicate='deadline.closes_on' AND superseded_at IS NULL "
         "AND value_state='stated'" + (" AND source_id=?" if sid else ""))
    return sorted(r[0] for r in svc.execute(q, (sid,) if sid else ()))


class WithdrawTests(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.addCleanup(self.svc.close)
        self.out = add_outlet(self.svc)
        self.sid = add_source(self.svc, outlet=self.out)
        promote.promote_raw(self.svc, _raw(self.svc, self.sid, CALL, "2026-11-01T00:00:00Z", "h1"), "2026-11-01T00:00:00Z")
        self.assertEqual(live_dates(self.svc), ["2026-12-31"])

    def test_membership_drive_rewrite_withdraws_deadline(self):
        now = "2026-11-10T00:00:00Z"
        stats = promote.promote_raw(self.svc, _raw(self.svc, self.sid, MEMBERSHIP, now, "h2"), now)
        self.assertEqual(stats.withdrawn, 1)
        self.assertEqual(live_dates(self.svc), [])
        row = self.svc.execute("SELECT value_state, correction_reason, correction_of FROM pb_fact WHERE superseded_at IS NULL "
                               "AND predicate='deadline.closes_on'").fetchone()
        self.assertEqual((row[0], row[1]), ("not_stated_in_inspected_scope", promote.WITHDRAW_REASON))
        self.assertIsNotNone(row[2])  # history kept: the old fact is superseded, not deleted
        self.assertEqual(self.svc.execute("SELECT count(*) FROM pb_fact WHERE predicate='deadline.closes_on'").fetchone()[0], 2)

    def test_same_deadline_kept(self):
        now = "2026-11-10T00:00:00Z"
        stats = promote.promote_raw(self.svc, _raw(self.svc, self.sid, CALL + " 문의", now, "h2"), now)
        self.assertEqual(stats.withdrawn, 0)
        self.assertEqual(live_dates(self.svc), ["2026-12-31"])

    def test_other_source_untouched(self):
        sid2 = add_source(self.svc, url="https://example.org/other", outlet=self.out)
        promote.promote_raw(self.svc, _raw(self.svc, sid2, OTHER, "2026-11-02T00:00:00Z", "o1"), "2026-11-02T00:00:00Z")
        now = "2026-11-10T00:00:00Z"
        promote.promote_raw(self.svc, _raw(self.svc, self.sid, MEMBERSHIP, now, "h2"), now)
        self.assertEqual(live_dates(self.svc, sid2), ["2027-01-15"])
        self.assertEqual(live_dates(self.svc, self.sid), [])

    def test_uninspected_raw_withdraws_nothing(self):
        now = "2026-11-10T00:00:00Z"
        add_revision(self.svc, self.sid, content_hash="h3", fetched_at=now)
        rid = store.insert_raw(self.svc, now, source_id=self.sid, url="https://example.org/x", kind="html", fetch_method="html",
                               outcome="ok", change_state="unchanged", text_hash="h3", fetched_at=now)
        self.assertEqual(promote.withdraw_unstated_deadlines(self.svc, rid, set(), now), 0)
        self.assertEqual(live_dates(self.svc), ["2026-12-31"])

    def test_restated_deadline_comes_back(self):
        now = "2026-11-10T00:00:00Z"
        promote.promote_raw(self.svc, _raw(self.svc, self.sid, MEMBERSHIP, now, "h2"), now)
        later = "2026-11-12T00:00:00Z"
        promote.promote_raw(self.svc, _raw(self.svc, self.sid, CALL, later, "h4"), later)
        self.assertEqual(live_dates(self.svc), ["2026-12-31"])


if __name__ == "__main__":
    unittest.main()
