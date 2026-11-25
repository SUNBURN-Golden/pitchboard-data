import json
import os
import unittest
from unittest.mock import patch

from helpers import NOW, add_source, service
from pbdata import db
from pbdata.collect import collector, store


RNOW = "2026-11-26T00:00:00Z"


class RetryTransientFirstFetchTests(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.inf = db.connect(":memory:")
        self.addCleanup(self.svc.close)
        self.addCleanup(self.inf.close)
        db.migrate(self.inf, "infra")
        self.site = store.register_site(self.svc, "https://retry.example", "Retry Review", NOW, target_id="t1")
        self.outlet = store.site_row(self.svc, self.site)["site_outlet"]
        self.ctx = collector.Ctx(self.svc, self.inf, None, None, {"targets": []}, "run_retry", "retry", lambda: RNOW)

    def page(self, name, **kw):
        sid = add_source(self.svc, "https://retry.example/" + name, outlet=self.outlet, **kw)
        return sid

    def fail(self, sid, fetched_at="2026-11-21T00:00:00Z", outcome="error", error_class="URLError", depth=3):
        url = self.svc.execute("SELECT canonical_url FROM pb_source WHERE source_id=?", (sid,)).fetchone()[0]
        return store.insert_raw(self.svc, fetched_at, source_id=sid, url=url, kind="html", fetch_method="html",
                                outcome=outcome, error_class=error_class, change_state="failed", depth=depth,
                                fetched_at=fetched_at)

    def ok(self, sid, fetched_at="2026-11-22T00:00:00Z"):
        url = self.svc.execute("SELECT canonical_url FROM pb_source WHERE source_id=?", (sid,)).fetchone()[0]
        return store.insert_raw(self.svc, fetched_at, source_id=sid, url=url, kind="html", fetch_method="html",
                                outcome="ok", change_state="new", depth=1, fetched_at=fetched_at)

    def jobs(self):
        return [(json.loads(r["payload"]), r) for r in self.inf.execute("SELECT * FROM job ORDER BY rowid")]

    def test_transient_first_fetch_failure_is_retried_once_due(self):
        sid = self.page("a")
        self.fail(sid)
        self.assertEqual(collector.enqueue_retries(self.ctx), 1)
        (p, row), = self.jobs()
        self.assertEqual(p, {"url": "https://retry.example/a", "site_source_id": self.site, "depth": 3, "via": "retry"})
        self.assertEqual(row["kind"], "fetch_page")
        self.assertEqual(row["priority"], collector.RETRY_PRIORITY)
        # idempotent while the same failure count stands
        self.assertEqual(collector.enqueue_retries(self.ctx), 0)

    def test_refresh_alone_never_revisits_a_never_ok_url(self):
        self.fail(self.page("a"))
        self.assertEqual(collector.refresh_candidates(self.svc, RNOW), [])

    def test_backoff_doubles_and_gives_up_after_max_failures(self):
        recent = self.page("recent")
        self.fail(recent, fetched_at="2026-11-25T12:00:00Z")            # 12h ago < 24h
        second = self.page("second")
        self.fail(second, fetched_at="2026-11-23T00:00:00Z")
        self.fail(second, fetched_at="2026-11-25T00:00:00Z")            # 2nd failure 24h ago < 48h
        third = self.page("third")
        self.fail(third, fetched_at="2026-11-20T00:00:00Z")
        self.fail(third, fetched_at="2026-11-23T00:00:00Z")             # 2nd failure 72h ago >= 48h
        dead = self.page("dead")
        for d in ("2026-11-01", "2026-11-05", "2026-11-10"):
            self.fail(dead, fetched_at=d + "T00:00:00Z")
        urls = [r["url"] for r in collector.retry_candidates(self.svc, RNOW)]
        self.assertEqual(urls, ["https://retry.example/third"])

    def test_deterministic_failures_and_fetched_sources_are_not_retried(self):
        for name, outcome, cls in (("robots", "denied_robots", "disallow"), ("gone", "not_found", "http_404"),
                                   ("wall", "login_wall", "login_required"), ("media", "error", "skipped_binary_media"),
                                   ("enc", "error", "UnicodeEncodeError"), ("http403", "denied_http", "http_403")):
            self.fail(self.page(name), outcome=outcome, error_class=cls)
        mixed = self.page("mixed")
        self.fail(mixed, fetched_at="2026-11-10T00:00:00Z", outcome="not_found", error_class="http_404")
        self.fail(mixed)
        fetched = self.page("fetched")
        self.fail(fetched)
        self.ok(fetched)
        manual = self.page("manual", permission="manual_only")
        self.fail(manual)
        retired = self.page("retired")
        self.fail(retired)
        self.svc.execute("UPDATE pb_source SET retired_reason='share_variant' WHERE source_id=?", (retired,))
        self.assertEqual(collector.retry_candidates(self.svc, RNOW), [])

    def test_timeout_server_error_and_robots_unreachable_are_transient(self):
        self.fail(self.page("t"), outcome="timeout", error_class="timeout")
        self.fail(self.page("s"), outcome="server_error", error_class="http_503")
        self.fail(self.page("r"), outcome="error", error_class="robots_unreachable")
        self.assertEqual(sorted(r["url"].rsplit("/", 1)[1] for r in collector.retry_candidates(self.svc, RNOW)), ["r", "s", "t"])

    def test_limit_env_caps_enqueued_retries(self):
        for n in range(5):
            self.fail(self.page("p%d" % n))
        with patch.dict(os.environ, {"PB_RETRY_FAILED_LIMIT": "2"}):
            self.assertEqual(collector.enqueue_retries(self.ctx), 2)
        self.assertEqual(collector.enqueue_retries(self.ctx, limit=0), 0)
        self.assertEqual(collector.enqueue_retries(self.ctx, limit=10), 3)

    def test_retry_job_is_not_dropped_by_site_page_cap(self):
        sid = self.page("capped")
        self.fail(sid)
        self.ctx.site_page_cap = 0
        collector.enqueue_retries(self.ctx)
        (p, row), = self.jobs()
        with patch.object(collector.executor, "fetch_page") as fp:
            fp.return_value = collector.executor.PageResult(p["url"], None, "skipped", "", "", skip_reason="stub")
            self.assertEqual(collector.h_fetch_page(self.ctx, p, row), {"skipped": "stub"})
            fp.assert_called_once()
            self.assertFalse(fp.call_args.kwargs["refresh"])


if __name__ == "__main__":
    unittest.main()
