"""Institutional off-topic pages (2026-11-27): HR/payroll/admissions pages and campus calendars/news on .edu hosts are
retired through retire_junk_sources when never fetched, are not registered as new page sources, and are not
round-robin refreshed unless they back a live call."""
import json
import unittest
from unittest.mock import Mock

from helpers import NOW, add_source, service
from pbdata import db
from pbdata.collect import collector, store
from pbdata.collect.net import off_topic_page_url

EDU = "https://www.kent.edu"


class Classifier(unittest.TestCase):
    def test_off_topic(self):
        for u in (EDU + "/hr/sos-payment-options", EDU + "/accountspayable/e-payables", EDU + "/people-and-culture/news/open-enrollment-opens-monday-oct-13",
                  EDU + "/index.php/admissions/news/admissions-updates-november-2021", EDU + "/facultyhandbook/academic-calendar",
                  EDU + "/today/news/food-pantry-opens-east-liverpool-campus", EDU + "/artscollege/calendar", EDU + "/stark/event/thanksgiving-break-campus-closed-4",
                  "https://utulsa.edu/about/offices/bursar/payment-plans/", "https://www.swansea.ac.uk/admissions/application-deadlines/",
                  "https://irishwritersunion.org/payment-options/"):
            self.assertEqual("off_topic_path", off_topic_page_url(u), u)
        self.assertEqual("relative_url", off_topic_page_url("/blog/engagements_hub_category/currently-open/"))

    def test_writing_pages_kept(self):
        for u in (EDU + "/wick/news/wick-poetry-center-commemorates-river-burning-interactive-poetry-exhibit-community-poem",
                  EDU + "/kent/news/deadline-june-30-nominate-your-favorite-faculty-member-outstanding-teaching-award",
                  EDU + "/magazine/wksu-celebrates-70-years-radio-excellence", "https://www.nashwaakreview.ca/calendar",
                  "https://example.org/news/open-call", "https://example.org/events/reading", "https://www.narrativemagazine.com/submission-guidelines",
                  "https://www.vassar.edu/english/news/creative-writing-prize", "https://example.edu/admissions/creative-writing-mfa",
                  "https://www.arko.or.kr/board/view/4053?cid=1"):
            self.assertIsNone(off_topic_page_url(u), u)


class Sources(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.addCleanup(self.svc.close)
        self.infra = db.connect(":memory:")
        self.addCleanup(self.infra.close)
        db.migrate(self.infra, "infra")
        self.site = store.register_site(self.svc, EDU, "Kent State", NOW, target_id="t1")
        self.outlet = store.site_row(self.svc, self.site)["site_outlet"]

    def state(self, sid):
        return tuple(self.svc.execute("SELECT retired_reason, deprioritized_reason FROM pb_source WHERE source_id=?", (sid,)).fetchone())

    def test_never_fetched_off_topic_source_is_deprioritized_not_deleted(self):
        from pbdata.collect import metrics
        junk = add_source(self.svc, EDU + "/hr/sos-payment-options", outlet=self.outlet)
        keep = add_source(self.svc, EDU + "/wick/news/poetry-contest", outlet=self.outlet)
        n = self.svc.execute("SELECT count(*) FROM pb_source").fetchone()[0]
        before = metrics.source_fetch_counts(self.svc)
        self.assertEqual(0, store.retire_junk_sources(self.svc))
        self.assertEqual(1, store.deprioritize_off_topic_sources(self.svc))
        self.assertEqual(0, store.deprioritize_off_topic_sources(self.svc))      # idempotent
        self.assertEqual((None, "off_topic_path"), self.state(junk))
        self.assertEqual((None, None), self.state(keep))
        self.assertEqual(n, self.svc.execute("SELECT count(*) FROM pb_source").fetchone()[0])
        after = metrics.source_fetch_counts(self.svc)
        self.assertEqual(before["sources_never_fetched"] - 1, after["sources_never_fetched"])
        self.assertEqual(1, after["sources_deprioritized_off_topic"])

    def test_deprioritized_source_gets_no_retry(self):
        junk = add_source(self.svc, EDU + "/hr/pay", outlet=self.outlet)
        store.insert_raw(self.svc, "2020-01-01T00:00:00Z", source_id=junk, url=EDU + "/hr/pay", kind="html", fetch_method="html",
                         outcome="error", error_class="URLError", change_state="failed", fetched_at="2020-01-01T00:00:00Z")
        self.assertEqual([EDU + "/hr/pay"], [r["url"] for r in collector.retry_candidates(self.svc, NOW)])
        store.deprioritize_off_topic_sources(self.svc)
        self.assertEqual([], collector.retry_candidates(self.svc, NOW))

    def test_new_off_topic_page_is_not_registered(self):
        self.assertIsNone(store.ensure_page_source(self.svc, EDU + "/accountspayable/e-payables", self.site, NOW))
        self.assertIsNotNone(store.ensure_page_source(self.svc, EDU + "/wick/submissions", self.site, NOW))

    def test_queued_refresh_of_off_topic_page_is_skipped_without_fetch(self):
        fetcher = Mock()
        ctx = collector.Ctx(self.svc, self.infra, fetcher, None, {"targets": []}, "run_x", "x", lambda: NOW)
        res = collector.h_fetch_page(ctx, {"url": EDU + "/hr/sos-payment-options", "site_source_id": self.site, "via": "refresh"}, None)
        self.assertEqual({"skipped": "off_topic_path"}, res)
        fetcher.fetch.assert_not_called()

    def test_refresh_candidates_drop_round_robin_off_topic_rows(self):
        for path in ("/hr/pay-range", "/wick/submissions"):
            sid = add_source(self.svc, EDU + path, outlet=self.outlet)
            store.insert_raw(self.svc, NOW, source_id=sid, url=EDU + path, kind="html", fetch_method="html", outcome="ok",
                             change_state="new", fetched_at=NOW, text_hash="h" + path)
        urls = [r["url"] for r in collector.refresh_candidates(self.svc, NOW)]
        self.assertEqual([EDU + "/wick/submissions"], urls)


if __name__ == "__main__":
    unittest.main()
