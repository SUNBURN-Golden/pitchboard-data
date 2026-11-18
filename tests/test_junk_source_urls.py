import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from helpers import NOW, add_revision, add_source, service
from pbdata import db, facts
from pbdata.collect import collector, metrics, store
from pbdata.collect.net import FetchResult, junk_page_url, strip_tracking_params
from pbdata.daemon.stages import s_incremental_fetch, s_quality_check


ORIGIN = "https://example.org"


class JunkPageURLTests(unittest.TestCase):
    def test_tracking_params_strip_without_reencoding_board_query(self):
        board = ORIGIN + "/board/view?bbsIdx=%ED%95%9C%EA%B8%80%20%2B&pageIndex=1&filter=&flag&filter=a+b"
        self.assertEqual(strip_tracking_params(board), board)
        self.assertIsNone(junk_page_url(board))
        for tracking in ("share=twitter", "share=facebook", "amp-share=true", "replytocom=12", "UTM_Source=x",
                         "utm_campaign", "fbclid=foo", "%67clid=foo"):
            with self.subTest(tracking=tracking):
                variant = board.replace("&pageIndex", "&" + tracking + "&pageIndex") + "#section"
                self.assertEqual(junk_page_url(variant), "share_variant")
                self.assertEqual(strip_tracking_params(variant), board + "#section")

    def test_binary_suffixes_use_path_and_keep_guideline_documents(self):
        for path in ("image.JPEG", "image.png", "logo%2Esvg", "movie.webm", "sound.mp3", "file.tar.gz", "font.woff2", "main.js"):
            self.assertEqual(junk_page_url(ORIGIN + "/" + path + "?size=2"), "binary_asset", path)
        for path in ("rules.pdf", "rules.hwp", "rules.doc", "rules.docx", "board?file=image.png", "view?shareholder=1"):
            self.assertIsNone(junk_page_url(ORIGIN + "/" + path), path)


class JunkSourceTests(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.addCleanup(self.svc.close)
        self.infra = db.connect(":memory:")
        self.addCleanup(self.infra.close)
        db.migrate(self.infra, "infra")
        self.site = store.register_site(self.svc, ORIGIN, "Example Review", NOW, target_id="t1")
        self.outlet = store.site_row(self.svc, self.site)["site_outlet"]
        self.fetcher = Mock()
        self.ctx = collector.Ctx(self.svc, self.infra, self.fetcher, None, {"targets": []}, "run_junk", "junk", lambda: NOW)

    def legacy_source(self, path, **kw):
        return add_source(self.svc, ORIGIN + path, outlet=self.outlet, **kw)

    def raw(self, sid, url, outcome="ok", kind="html"):
        return store.insert_raw(self.svc, NOW, source_id=sid, url=ORIGIN + url, kind=kind, fetch_method="html",
                                outcome=outcome, change_state="new" if outcome == "ok" else "failed", fetched_at=NOW)

    def fetch_page(self, path, via="link"):
        return collector.h_fetch_page(self.ctx, {"url": ORIGIN + path, "site_source_id": self.site, "via": via}, None)

    def queued_urls(self):
        return [json.loads(row[0])["url"] for row in self.infra.execute("SELECT payload FROM job WHERE kind='fetch_page'")]

    def test_page_registration_canonicalizes_before_dedup_and_filters_assets(self):
        def register(path):
            return store.ensure_page_source(self.svc, ORIGIN + path, self.site, NOW)

        sid = register("/submit?share=twitter")
        self.assertIsNotNone(sid)
        self.assertEqual(register("/submit?share=facebook"), sid)
        self.assertEqual(register("/submit"), sid)
        self.assertIsNone(register("/submit/banner.jpeg?utm_source=site"))
        self.assertIsNone(register("/submit/notice.png"))
        self.assertIsNotNone(register("/submit/rules.pdf"))
        self.assertIsNotNone(register("/submit/rules.hwp"))
        self.assertEqual({r[0] for r in self.svc.execute("SELECT canonical_url FROM pb_source")},
                         {ORIGIN + p for p in ("/", "/submit", "/submit/rules.pdf", "/submit/rules.hwp")})

    def test_follow_and_sitemap_enqueue_one_canonical_url_and_skip_assets(self):
        links = ("/submission?share=twitter", "/submission?share=facebook", "/submission", "/submission/banner.jpeg", "/submission/notice.png",
                 "/submission/rules.pdf?utm_source=site", "/submission/rules.hwp", "/submission?bbsIdx=17&pageIndex=1")
        root = ORIGIN + "/"
        body = "<h1>Submission guidelines</h1>" + "".join(f'<a href="{path}">Submission guidelines</a>' for path in links)
        self.fetcher.fetch.return_value = FetchResult(root, root, 200, "text/html", body.encode(), "ok")
        self.fetch_page("/")
        expected = {ORIGIN + p for p in ("/submission", "/submission/rules.pdf", "/submission/rules.hwp", "/submission?bbsIdx=17&pageIndex=1")}
        self.assertCountEqual(self.queued_urls(), expected)
        self.infra.execute("DELETE FROM job")
        with patch.object(collector, "_sitemap_urls", return_value=[ORIGIN + p for p in links]):
            collector.h_expand_site(self.ctx, {"site_source_id": self.site}, None)
        self.assertCountEqual(self.queued_urls(), expected)

    def test_retirement_keeps_all_rows_and_protects_each_kind_of_history(self):
        share = self.legacy_source("/submit?share=twitter")
        image = self.legacy_source("/rules.png")
        self.raw(image, "/rules.png", outcome="timeout")
        evidence_only = self.legacy_source("/evidence.jpeg")
        rev = add_revision(self.svc, evidence_only)
        facts.add_evidence(self.svc, rev, evidence_only, "Submissions are open.", "Rules", 0, 0, NOW)
        fact_only = self.legacy_source("/fact?utm_source=old")
        self.svc.execute("INSERT INTO pb_fact(fact_id,subject_type,subject_id,predicate,value_state,epistemic,source_id,"
                         "source_authority,collected_at,recorded_at,extractor,extractor_version) "
                         "VALUES('fact_test','source',?,'status.declared','not_inspected','unverified',?,"
                         "'official_primary',?,?,'rules','1')", (fact_only, fact_only, NOW, NOW))
        successful_raw = self.legacy_source("/success.png")
        self.raw(successful_raw, "/success.png")
        self.svc.execute("UPDATE pb_source SET last_success_at=NULL WHERE source_id=?", (successful_raw,))
        timestamp_only = self.legacy_source("/old?share=twitter")
        self.svc.execute("UPDATE pb_source SET last_success_at=? WHERE source_id=?", (NOW, timestamp_only))
        self.legacy_source("/clip.png", source_type="manual_clip", permission="manual_only")
        self.legacy_source("/rules.pdf")
        # Site identity comes from pb_source_site, even if a historical root URL looks like a share variant.
        self.svc.execute("UPDATE pb_source SET canonical_url=? WHERE source_id=?", (ORIGIN + "/?share=twitter", self.site))
        before = {r[0] for r in self.svc.execute("SELECT source_id FROM pb_source")}

        self.assertEqual(store.retire_junk_sources(self.svc), 2)
        self.assertEqual(store.retire_junk_sources(self.svc), 0)
        self.assertEqual(dict(self.svc.execute("SELECT source_id, retired_reason FROM pb_source WHERE retired_reason IS NOT NULL")),
                         {share: "share_variant", image: "binary_asset"})
        self.assertEqual({r[0] for r in self.svc.execute("SELECT source_id FROM pb_source")}, before)
        self.assertEqual(self.svc.execute("SELECT count(*) FROM pb_raw_ref").fetchone()[0], 2)

    def test_backlog_skips_retired_and_new_binary_urls_without_fetching(self):
        self.legacy_source("/submit?share=twitter")
        store.retire_junk_sources(self.svc)
        self.assertEqual(self.fetch_page("/submit?share=twitter"), {"skipped": "retired_source"})
        self.assertEqual(self.fetch_page("/new.png"), {"skipped": "binary_asset"})
        self.assertEqual(self.svc.execute("SELECT count(*) FROM pb_source").fetchone()[0], 2)
        self.fetcher.fetch.assert_not_called()

    def test_refresh_skips_retired_but_keeps_historical_successful_image(self):
        retired = self.legacy_source("/retired.png")
        active = self.legacy_source("/active.png")
        self.raw(active, "/active.png", kind="image")
        store.retire_junk_sources(self.svc)
        # Even if successful raw history appeared after retirement, the retired source stays out of refresh.
        self.raw(retired, "/retired.png", kind="image")
        self.assertEqual(collector.enqueue_refresh(self.ctx), 1)
        self.assertEqual(self.queued_urls(), [ORIGIN + "/active.png"])
        self.assertEqual(self.fetch_page("/retired.png", "refresh"), {"skipped": "retired_source"})
        url = ORIGIN + "/active.png"
        self.fetcher.fetch.return_value = FetchResult(url, url, 500, "image/png", b"", "server_error")
        self.assertEqual(self.fetch_page("/active.png", "refresh")["outcome"], "server_error")
        self.assertEqual(self.fetcher.fetch.call_args.args[0], url)

    def test_daemon_reports_new_retirements_and_both_metrics_exports(self):
        self.legacy_source("/old.jpeg")
        old = self.legacy_source("/historical")
        self.raw(old, "/historical")
        self.svc.execute("UPDATE pb_source SET last_success_at=NULL WHERE source_id=?", (old,))
        c = SimpleNamespace(svc=self.svc, infra=self.infra, clock=lambda: NOW, run_id="test_junk",
                            opts={"offline": True}, state={}, root="unused")
        with patch("pbdata.daemon.stages.clips.ingest_dir", return_value={}):
            self.assertEqual(s_incremental_fetch(c)["junk_sources_retired"], 1)
        daemon = s_quality_check(c)
        exported = metrics.compute(self.svc, self.infra, {"targets": []})
        for report in (daemon, exported):
            self.assertEqual(report["sources_never_fetched"], 1)
            self.assertEqual(report["sources_retired_junk"], 1)
        self.assertEqual(daemon["sources_never_succeeded"], 1)
