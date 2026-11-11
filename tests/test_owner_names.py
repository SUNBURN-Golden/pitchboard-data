"""Owner/outlet naming rules: non-names (CTA labels, bare QIDs, empty/URL-only) are rejected and fall back
to better available names. Tuning examples are the two live junk owners ("온라인 접수하기", "Q121252827");
HELDOUT_GOOD / HELDOUT_JUNK below were not used while tuning the rule."""
import unittest
from unittest import mock

from helpers import NOW, add_call, add_outlet, add_source, service
from pbdata import db
from pbdata.collect import collector, discover, evaluate, names, parse, store
from pbdata.collect.net import Fetcher, FetchResult
from pbdata.collect.wiki import Api
from pbdata.daemon import project

# real owner/outlet names currently in the service DB (Korean and English) -- must never be changed
HELDOUT_GOOD = [
    "금성출판사", "한국문학번역원 공지", "한국문화예술위원회 공지/공모", "서울문화재단 공지/공모",
    "문장웹진(한국문화예술위원회)", "글여울",
    "Narrative Magazine", "New England Review", "Geist", "Douglas College", "Vassar College",
    "Gimm-Young Publishers", "Writer's Digest Competitions", "Korean Children's Book Association",
    "Going Down Swinging", "Rue Morgue", "Taddle Creek", "Plumwood Mountain",
]
# junk strings NOT used while tuning (tuning saw only the two live examples)
HELDOUT_JUNK = [
    "지금 신청", "Learn more", "Q4115189", "홈페이지 바로가기", "Submit here",
    "더 보기", "Apply online", "Q27948", "Click here to apply", "지원하기",
]


class JunkRuleTests(unittest.TestCase):
    def test_live_tuning_examples_are_junk(self):
        self.assertTrue(names.is_junk_name("온라인 접수하기"))
        self.assertTrue(names.is_junk_name("Q121252827"))

    def test_heldout_junk_is_caught(self):
        for s in HELDOUT_JUNK:
            self.assertTrue(names.is_junk_name(s), s)
            self.assertIsNone(names.clean_name(s), s)

    def test_empty_and_url_only_are_junk(self):
        for s in (None, "", "   ", "\t\n", "https://weinovel.com/contest", "http://x.org", "www.example.com", "example.com/apply"):
            self.assertTrue(names.is_junk_name(s), repr(s))

    def test_cta_variants_are_junk(self):
        for s in ("접수하기", "신청하기", "바로가기", "더보기", "공모전 접수하기", "사이트 바로가기", "자세히 보기",
                  "Apply now", "SUBMIT", "apply online!", "Read more", "Sign up", "Submit your story", "접수하기 →", "  Learn more  "):
            self.assertTrue(names.is_junk_name(s), s)

    def test_heldout_good_names_pass(self):
        for s in HELDOUT_GOOD:
            self.assertFalse(names.is_junk_name(s), s)
            self.assertEqual(names.clean_name(s), s)

    def test_tricky_real_names_pass(self):
        # slash inside a real name is not a URL; punctuation/parentheses/case are preserved
        for s in ("Doek! Literary Magazine", "Tulsa Studies in Women's Literature", "GigaNotoSaurus", "ParSec",
                  "Mascara", "Pak Kyong-ni Prize", "Lovecraft Annual", "Analog Science Fiction and Fact"):
            self.assertFalse(names.is_junk_name(s), s)

    def test_clean_name_normalises_whitespace_only(self):
        self.assertEqual(names.clean_name("  Narrative   Magazine "), "Narrative Magazine")
        self.assertEqual(names.clean_name("Ｎａｒｒａｔｉｖｅ"), "Narrative")   # NFKC


class FallbackTests(unittest.TestCase):
    def test_host_name(self):
        self.assertEqual(names.host_name("www.khoreomag.com"), "khoreomag.com")
        self.assertEqual(names.host_name("https://www.weinovel.com/contest"), "weinovel.com")
        self.assertEqual(names.host_name("www.sfac.or.kr"), "sfac.or.kr")
        self.assertEqual(names.host_name("example.com:8080"), "example.com")
        self.assertEqual(names.host_name(""), "")

    def test_title_brand(self):
        self.assertEqual(names.title_brand("Khoreo – A Journal of Poetry and Prose"), "Khoreo")
        self.assertEqual(names.title_brand("Submit Your Story | Narrative Magazine"), "Narrative Magazine")
        self.assertEqual(names.title_brand("공모전 접수 | weinovel", "weinovel.com"), "weinovel")
        self.assertEqual(names.title_brand("Narrative Magazine"), "Narrative Magazine")
        self.assertIsNone(names.title_brand("Apply now | Submit here"))
        self.assertIsNone(names.title_brand(""))

    def test_doc_names_prefers_site_name_meta(self):
        doc = parse.parse_html('<html><head><meta property="og:site_name" content="weinovel">'
                               "<title>공모전 접수 안내 | weinovel</title></head><body><p>x</p></body></html>".encode("utf-8"),
                               "https://weinovel.com/contest")
        self.assertEqual(names.doc_names(doc, "weinovel.com"), ["weinovel"])
        doc = parse.parse_html(b"<html><head><title>Khoreo - A Journal of Poetry</title></head><body><p>x</p></body></html>",
                               "https://www.khoreomag.com/")
        self.assertEqual(names.doc_names(doc, "www.khoreomag.com"), ["Khoreo"])

    def test_pick_name_order_and_host_fallback(self):
        self.assertEqual(names.pick_name(["온라인 접수하기", "Q121252827", "weinovel"], "weinovel.com"), "weinovel")
        self.assertEqual(names.pick_name(["Q121252827"], "www.khoreomag.com"), "khoreomag.com")
        self.assertEqual(names.pick_name([None, "", "  "], "https://www.weinovel.com"), "weinovel.com")
        self.assertEqual(names.pick_name(["Geist"], "geist.com"), "Geist")          # good label wins over everything
        self.assertEqual(len(names.pick_name(["x" * 300], "h.com")), 120)


class RegisterSiteTests(unittest.TestCase):
    def setUp(self):
        self.svc = service()

    def registered(self, label, alt=(), origin="https://weinovel.com"):
        sid = store.register_site(self.svc, origin, label, NOW, target_id="t1", alt_names=alt)
        return self.svc.execute("SELECT o.canonical_name, s.owner_name FROM pb_source s JOIN pb_outlet o ON o.outlet_id=s.owner_outlet_id "
                                "WHERE s.source_id=?", (sid,)).fetchone()

    def test_good_label_unchanged(self):
        r = self.registered("Narrative Magazine", origin="https://www.narrativemagazine.com")
        self.assertEqual((r["canonical_name"], r["owner_name"]), ("Narrative Magazine", "Narrative Magazine"))

    def test_cta_label_falls_back_to_alt_name(self):
        r = self.registered("온라인 접수하기", alt=["weinovel"])
        self.assertEqual((r["canonical_name"], r["owner_name"]), ("weinovel", "weinovel"))

    def test_qid_label_falls_back_to_host(self):
        r = self.registered("Q121252827", origin="https://www.khoreomag.com")
        self.assertEqual((r["canonical_name"], r["owner_name"]), ("khoreomag.com", "khoreomag.com"))

    def test_empty_label_falls_back_to_host(self):
        r = self.registered("", origin="https://www.khoreomag.com")
        self.assertEqual(r["canonical_name"], "khoreomag.com")


class _StubFetcher:
    def __init__(self, pages):
        self.pages, self.requests = pages, 0

    def fetch(self, url, **kw):
        self.requests += 1
        body = self.pages.get(url)
        if body is None:
            return FetchResult(url, url, 404, "text/html", b"", "not_found")
        return FetchResult(url, url, 200, "text/html", body, "ok")


class DiscoveryNamingTests(unittest.TestCase):
    def test_directory_hub_cta_link_text_is_not_a_label(self):
        page = ('<html><body><a href="https://weinovel.com/contest">온라인 접수하기</a>'
                '<a href="https://example-prize.org/call">Example Poetry Prize</a></body></html>').encode("utf-8")
        f = _StubFetcher({"https://directory.example/list": page})
        cands = discover.call_index(f, {"id": "t1", "call_indexes": [{"url": "https://directory.example/list", "kind": "directory"}]}, NOW, 2026)
        by_host = {c.url.split("/")[2]: c.label for c in cands}
        self.assertEqual(by_host["weinovel.com"], "")                       # CTA dropped at the source
        self.assertEqual(by_host["example-prize.org"], "Example Poetry Prize")

    def test_entity_without_label_never_yields_qid(self):
        api = Api(min_interval=0, sleep=lambda *_: None)
        api.get = lambda *a, **k: {"entities": {"Q121252827": {"labels": {}, "descriptions": {},
                                                               "claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://www.khoreomag.com/"}}}]}}}}
        self.assertEqual(api.entity_sites(["Q121252827"])["Q121252827"]["label"], "")
        api.get = lambda *a, **k: {"entities": {"Q121252827": {"labels": {"ko": {"value": "코레오"}}, "descriptions": {},
                                                               "claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://www.khoreomag.com/"}}}]}}}}
        self.assertEqual(api.entity_sites(["Q121252827"])["Q121252827"]["label"], "코레오")

    def test_sparql_qid_label_is_dropped(self):
        api = Api(min_interval=0, sleep=lambda *_: None)
        api.sparql = lambda q: [
            {"item": {"value": "http://www.wikidata.org/entity/Q121252827"}, "itemLabel": {"value": "Q121252827"},
             "site": {"value": "https://www.khoreomag.com/"}},
            {"item": {"value": "http://www.wikidata.org/entity/Q42"}, "itemLabel": {"value": "Khoreo"},
             "site": {"value": "https://example.org/"}},
        ]
        cands = discover.wikidata_class(api, {"id": "t1", "country_qid": "Q884", "organizer_types": ["literary magazine"]})
        self.assertEqual([c.label for c in cands], ["", "Khoreo"])
        self.assertEqual(cands[0].qid, "Q121252827")                        # QID kept as identity, not as name


class EvaluateNamingTests(unittest.TestCase):
    def setUp(self):
        self.svc = service()
        self.inf = db.connect(":memory:")
        db.migrate(self.inf, "infra")

    def evaluate_candidate(self, label, html, origin="https://weinovel.com"):
        url = origin + "/contest"
        self.svc.execute("INSERT INTO pb_discovery_candidate(cand_id,url,host,label,target_id,strategy,origin_ref,depth,prior_relevance,state,discovered_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         ("cand_x", url, origin.split("/")[2], label, "t1", "directory_hub", "t1:https://directory.example/list", 0, 0.6, "new", NOW))
        doc = parse.parse_html(html, url)
        fr = FetchResult(url, url, 200, "text/html", b"", "ok")
        ev = evaluate.Evaluation("registered", "ok", {"relevance": 0.8}, {url: fr}, {url: doc}, 1, [], [])
        ctx = collector.Ctx(self.svc, self.inf, Fetcher(self.inf, allow_loopback=True, sleep=lambda s: None), Api(cap=0),
                            {"targets": [{"id": "t1", "language": "ko"}]}, "run_t", "t", lambda: NOW)
        with mock.patch.object(evaluate, "trial_crawl", return_value=ev):
            out = collector.h_evaluate(ctx, {"cand_id": "cand_x"}, None)
        self.assertEqual(out["state"], "registered")
        return self.svc.execute("SELECT o.canonical_name FROM pb_outlet o JOIN pb_source_site ss USING(outlet_id)").fetchone()[0]

    def test_cta_label_uses_og_site_name(self):
        name = self.evaluate_candidate("온라인 접수하기",
                                       '<html><head><meta property="og:site_name" content="weinovel"><title>공모전 접수</title></head><body><p>x</p></body></html>'.encode("utf-8"))
        self.assertEqual(name, "weinovel")

    def test_qid_label_uses_title_brand(self):
        name = self.evaluate_candidate("Q121252827",
                                       b"<html><head><title>Khoreo - A Journal of Poetry and Prose</title></head><body><p>x</p></body></html>",
                                       origin="https://www.khoreomag.com")
        self.assertEqual(name, "Khoreo")

    def test_all_junk_falls_back_to_host(self):
        name = self.evaluate_candidate("접수하기", b"<html><head><title>Apply now | Submit here</title></head><body><p>x</p></body></html>")
        self.assertEqual(name, "weinovel.com")

    def test_good_label_wins_over_page_title(self):
        name = self.evaluate_candidate("Narrative Magazine", b"<html><head><title>Some Page Title</title></head><body><p>x</p></body></html>",
                                       origin="https://www.narrativemagazine.com")
        self.assertEqual(name, "Narrative Magazine")


class ProjectionNamingTests(unittest.TestCase):
    def setUp(self):
        self.svc = service()

    def test_junk_canonical_displays_host_name(self):
        oid = add_outlet(self.svc, "온라인 접수하기")
        sid = add_source(self.svc, "https://weinovel.com/contest", outlet=oid)
        self.svc.execute("INSERT INTO pb_source_site(source_id,outlet_id,origin,registered_domain,in_seed_list,target_id) VALUES(?,?,?,?,0,?)",
                         (sid, oid, "https://weinovel.com", "weinovel.com", "t1"))
        cid = add_call(self.svc, oid)
        self.assertEqual(project.build_payload(self.svc, cid)["owner"]["name"], "weinovel.com")

    def test_good_canonical_unchanged(self):
        oid = add_outlet(self.svc, "Geist")
        cid = add_call(self.svc, oid)
        self.assertEqual(project.build_payload(self.svc, cid)["owner"]["name"], "Geist")

    def test_junk_without_host_keeps_canonical(self):
        # nothing better available: never invent a name
        oid = add_outlet(self.svc, "Q121252827")
        cid = add_call(self.svc, oid)
        self.assertEqual(project.build_payload(self.svc, cid)["owner"]["name"], "Q121252827")


if __name__ == "__main__":
    unittest.main()
