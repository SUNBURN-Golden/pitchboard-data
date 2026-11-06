"""Successful fetch bookkeeping and the quality metric on historical raw refs."""
from types import SimpleNamespace
import sqlite3
import unittest
from unittest.mock import Mock

from helpers import NOW, add_source, service
from pbdata import db
from pbdata.collect import executor, store
from pbdata.collect.net import FetchResult
from pbdata.daemon.stages import s_quality_check

LATER = "2026-10-28T00:00:00Z"


class SourceSuccessTest(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.addCleanup(self.svc.close)
        self.sid = add_source(self.svc)

    def last_success(self, sid=None):
        return self.svc.execute("SELECT last_success_at FROM pb_source WHERE source_id=?", (sid or self.sid,)).fetchone()[0]

    def raw(self, outcome="ok", fetched_at=NOW, **kw):
        return store.insert_raw(self.svc, LATER, source_id=self.sid, url="https://example.org/submit",
                                kind="html", fetch_method="html", outcome=outcome,
                                fetched_at=fetched_at, change_state="new" if outcome == "ok" else "failed", **kw)

    def test_ok_uses_raw_fetch_timestamp_and_preserves_other_sources(self):
        other = add_source(self.svc, "https://example.org/other")
        self.raw()
        self.assertEqual(self.last_success(), NOW)  # fetched_at, not the ID-generation time
        self.assertIsNone(self.last_success(other))

    def test_latest_success_survives_out_of_order_inserts(self):
        self.raw(fetched_at=LATER)
        self.raw(fetched_at=NOW)
        self.assertEqual(self.last_success(), LATER)

    def test_non_ok_neither_sets_nor_clears_success(self):
        for initial in (None, NOW):
            for outcome in ("timeout", "empty_body", "denied_http", "error"):
                with self.subTest(initial=initial, outcome=outcome):
                    self.svc.execute("UPDATE pb_source SET last_success_at=? WHERE source_id=?", (initial, self.sid))
                    self.raw(outcome, fetched_at=LATER)
                    self.assertEqual(self.last_success(), initial)

    def test_raw_without_source_is_still_supported(self):
        store.insert_raw(self.svc, NOW, url="https://example.org/unassigned", kind="html", fetch_method="html",
                         outcome="ok", fetched_at=NOW, change_state="new")
        self.assertIsNone(self.last_success())

    def test_raw_and_timestamp_roll_back_together(self):
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with db.transaction(self.svc):
                self.raw()
                self.assertEqual(self.last_success(), NOW)
                raise RuntimeError("abort")
        self.assertIsNone(self.last_success())
        self.assertEqual(self.svc.execute("SELECT count(*) FROM pb_raw_ref").fetchone()[0], 0)

    def test_timestamp_write_failure_rolls_back_standalone_insert(self):
        self.svc.execute("CREATE TRIGGER reject_success BEFORE UPDATE OF last_success_at ON pb_source "
                         "BEGIN SELECT RAISE(ABORT, 'reject success'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "reject success"):
            self.raw()
        self.assertEqual(self.svc.execute("SELECT count(*) FROM pb_raw_ref").fetchone()[0], 0)
        self.assertIsNone(self.last_success())

    def fetch(self, outcome, now, body=b"", status=200):
        url = "https://example.org/submit"
        fetcher = Mock()
        fetcher.fetch.return_value = FetchResult(url, url, status, "text/html", body, outcome)
        return executor.fetch_page(self.svc, fetcher, url, self.sid, now)

    def test_executor_new_unchanged_and_changed_ok_advance_timestamp(self):
        body = b"<html><body><h1>Guidelines</h1><p>We welcome book reviews.</p></body></html>"
        for expected, now, content in (("new", NOW, body), ("unchanged", LATER, body),
                                       ("changed", "2026-10-29T00:00:00Z", body.replace(b"book", b"poetry"))):
            with self.subTest(change=expected):
                result = self.fetch("ok", now, content)
                self.assertEqual(result.outcome, "ok")
                self.assertEqual(result.change_state, expected)
                self.assertEqual(self.last_success(result.source_id), now)

    def test_executor_failed_or_empty_neither_sets_nor_clears_success(self):
        for initial in (None, NOW):
            for outcome, body, expected in (("timeout", b"", "timeout"), ("ok", b"<html><body></body></html>", "empty_body")):
                with self.subTest(initial=initial, outcome=outcome):
                    self.svc.execute("UPDATE pb_source SET last_success_at=? WHERE source_id=?", (initial, self.sid))
                    result = self.fetch(outcome, LATER, body)
                    self.assertEqual(result.outcome, expected)
                    self.assertEqual(self.last_success(result.source_id), initial)


class NeverSucceededMetricTest(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.infra = db.connect(":memory:")
        db.migrate(self.infra, "infra")
        self.addCleanup(self.svc.close)
        self.addCleanup(self.infra.close)

    def metric(self):
        c = SimpleNamespace(svc=self.svc, infra=self.infra, clock=lambda: NOW, run_id="metric",
                            opts={"offline": True}, state={})
        return s_quality_check(c)["sources_never_succeeded"]

    def source(self, label, permission="allowed_public", outcomes=(), last_success=None):
        url = "https://example.org/" + label
        sid = add_source(self.svc, url, permission=permission)
        for i, outcome in enumerate(outcomes):
            # Direct SQL deliberately reproduces historical writers that never set last_success_at.
            self.svc.execute("INSERT INTO pb_raw_ref(raw_id,source_id,url,kind,fetch_method,outcome,change_state,fetched_at) "
                             "VALUES(?,?,?,?,?,?,?,?)", (label + str(i), sid, url, "html", "html", outcome,
                                                       "new" if outcome == "ok" else "failed", NOW))
        self.svc.execute("UPDATE pb_source SET last_success_at=? WHERE source_id=?", (last_success, sid))
        return sid

    def test_historical_ok_raws_excluded_even_with_null_timestamp(self):
        for permission in ("allowed_public", "allowed_conditional"):
            self.source(permission, permission, ["ok", "ok", "timeout", "empty_body"])
        self.source("marked", outcomes=["ok"], last_success=NOW)
        self.assertEqual(self.metric(), 0)

    def test_unattempted_and_only_failed_sources_still_count(self):
        self.source("unattempted")
        self.source("failed", "allowed_conditional", ["timeout", "empty_body"])
        self.source("success", outcomes=["ok"])
        self.assertEqual(self.metric(), 2)

    def test_only_collectable_permissions_count(self):
        for permission in ("manual_only", "not_collectable", "pending_review"):
            self.source(permission + "_unattempted", permission)
            self.source(permission + "_ok", permission, ["ok"])
        self.source("allowed")
        self.assertEqual(self.metric(), 1)

    def test_raw_history_is_authoritative_over_timestamp_alone(self):
        self.source("timestamp_only", last_success=NOW)
        self.assertEqual(self.metric(), 1)


if __name__ == "__main__":
    unittest.main()
