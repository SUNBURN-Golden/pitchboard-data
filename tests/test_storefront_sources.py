"""Web-shop listing pages (2026-11-28): a coffeehousepress.org sitemap walk registered 662 Shopify book product and
collection pages; a 429 halted the host and they all stayed never fetched. Product/collection/cart pages are off-topic
even when the book title holds a writing word; a call word in the slug keeps the page."""
import unittest
from unittest.mock import Mock

from helpers import NOW, add_source, service
from pbdata import db
from pbdata.collect import collector, executor, metrics, store
from pbdata.collect.net import off_topic_page_url, storefront_page_url

SHOP = "https://coffeehousepress.org"


class Classifier(unittest.TestCase):
    def test_shop_listings_are_off_topic(self):
        for u in (SHOP + "/products/you", SHOP + "/products/written-after-a-massacre-in-the-year-2018",
                  SHOP + "/collections/poetry", SHOP + "/collections/all?page=3", SHOP + "/en/products/the-stories",
                  SHOP + "/cart", SHOP + "/account/login", "https://example.com/product/a-poetry-anthology/",
                  "https://example.com/product-category/books/fiction/"):
            self.assertTrue(storefront_page_url(u), u)
            self.assertEqual("off_topic_path", off_topic_page_url(u), u)

    def test_call_and_content_pages_kept(self):
        for u in (SHOP + "/pages/submissions", SHOP + "/blogs/news/pip-adam-wins-the-ursula-k-le-guin-award-for-audition", SHOP + "/",
                  SHOP + "/products/2027-chapbook-contest-entry", "https://example.com/product/poetry-prize-entry-fee/",
                  "https://example.com/shop/submission-fee", "https://example.org/issues/products-of-desire",
                  "https://example.org/blog/product-review", "https://www.narrativemagazine.com/submission-guidelines",
                  "https://www.arko.or.kr/board/view/4053?cid=1"):
            self.assertIsNone(off_topic_page_url(u), u)


class Sources(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.addCleanup(self.svc.close)
        self.site = store.register_site(self.svc, SHOP, "Coffee House Press", NOW, target_id="t1")
        self.outlet = store.site_row(self.svc, self.site)["site_outlet"]

    def test_never_fetched_product_pages_leave_never_fetched_metric(self):
        prod = add_source(self.svc, SHOP + "/products/you", outlet=self.outlet)
        keep = add_source(self.svc, SHOP + "/pages/submissions", outlet=self.outlet)
        before = metrics.source_fetch_counts(self.svc)
        self.assertEqual(1, store.deprioritize_off_topic_sources(self.svc))
        after = metrics.source_fetch_counts(self.svc)
        self.assertEqual(before["sources_never_fetched"] - 1, after["sources_never_fetched"])
        state = lambda s: self.svc.execute("SELECT deprioritized_reason FROM pb_source WHERE source_id=?", (s,)).fetchone()[0]
        self.assertEqual("off_topic_path", state(prod))
        self.assertIsNone(state(keep))

    def test_fetched_product_page_is_not_deprioritized(self):
        sid = add_source(self.svc, SHOP + "/products/fetched", outlet=self.outlet)
        store.insert_raw(self.svc, NOW, source_id=sid, url=SHOP + "/products/fetched", kind="html", fetch_method="html", outcome="ok",
                         change_state="new", fetched_at=NOW, text_hash="h")
        self.assertEqual(0, store.deprioritize_off_topic_sources(self.svc))

    def test_new_product_page_not_registered(self):
        self.assertIsNone(store.ensure_page_source(self.svc, SHOP + "/products/new-book", self.site, NOW))
        self.assertIsNotNone(store.ensure_page_source(self.svc, SHOP + "/pages/submissions", self.site, NOW))

    def test_deprioritized_page_found_again_by_sitemap_is_not_fetched(self):
        add_source(self.svc, SHOP + "/products/you", outlet=self.outlet)
        store.deprioritize_off_topic_sources(self.svc)
        self.assertIsNone(store.ensure_page_source(self.svc, SHOP + "/products/you", self.site, NOW))
        fetcher = Mock()
        r = executor.fetch_page(self.svc, fetcher, SHOP + "/products/you", self.site, NOW, run_id="r")
        self.assertEqual("skipped", r.outcome)
        fetcher.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
