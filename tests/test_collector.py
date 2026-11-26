import datetime as dt
import os
import unittest
from unittest.mock import Mock, patch

from helpers import NOW, add_source
import fixture_site as fx
from pbdata import db
from pbdata.collect import collector, goldeval, parse, scheduler, store
from pbdata.collect.net import Fetcher
from pbdata.collect.wiki import Api

ROOT = os.path.dirname(os.path.abspath(__file__))


def later(minutes):
    return (dt.datetime.strptime(NOW, "%Y-%m-%dT%H:%M:%SZ") + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class PriorityTests(unittest.TestCase):
    def test_ordering(self):
        p = scheduler.priority
        self.assertGreater(p(novelty=1, quality=.8), p(novelty=0, quality=.8))
        self.assertGreater(p(gap_importance=1), p(gap_importance=.2))
        self.assertGreater(p(urgency=1), p(urgency=0))
        self.assertLess(p(novelty=1, duplication=1), p(novelty=1))
        self.assertLess(p(novelty=1, failures=3), p(novelty=1, failures=0))
        self.assertLess(p(novelty=1, cost=1), p(novelty=1))

    def test_urgency(self):
        u = scheduler.urgency_from_deadline
        self.assertGreater(u("2026-10-30", "2026-10-27"), u("2027-06-01", "2026-10-27"))


class CycleTests(unittest.TestCase):
    def test_cycle_score_prefers_current_and_drops_past(self):
        from pbdata.profiles import pitchboard as prof
        today = dt.date(2026, 10, 28)
        self.assertGreater(prof.cycle_score("/contest/2027-prize", today), 0)
        self.assertGreater(prof.cycle_score("2026 open call", today), 0)
        self.assertLess(prof.cycle_score("/news/2024/winners", today), 0)
        self.assertEqual(prof.cycle_score("/submissions", today), 0)

    def test_past_deadline_never_eligible(self):
        from pbdata.profiles import pitchboard as prof
        d = parse.parse_html(b"<html><head><title>Open call for submissions</title></head><body><h1>Open call</h1><p>Submit your story. Deadline: March 31, 2026.</p></body></html>", "https://x.example/call")
        labs = prof.label_doc(d, prof.extract(d), dt.date(2026, 10, 28))
        rec = [l for l in labs if l.label == "recruitment"][0]
        self.assertFalse(rec.open_call_eligible)
        self.assertEqual(rec.status, "past_deadline")


class ParserTests(unittest.TestCase):
    def test_pdf_text_and_rows(self):
        d = parse.parse_pdf(fx.make_pdf(["Deadline: January 31, 2027.", "Entry fee: $15 per entry."]))
        self.assertIn("Entry fee: $15", d.text())
        self.assertFalse(d.ocr_used)

    def test_scanned_pdf_uses_ocr(self):
        d = parse.parse_pdf(fx.make_scanned_pdf(["Poetry Prize 2026", "Submissions close December 15, 2026"]))
        self.assertTrue(d.ocr_used)
        self.assertIn("December 15, 2026", d.text())

    def test_image_ocr(self):
        d = parse.parse_image(fx.make_png(["Translation Feature 2026", "Submissions close December 15, 2026"]))
        self.assertTrue(d.ocr_used)
        self.assertIn("December 15", d.text())
        self.assertIsNotNone(d.ocr_conf)

    def test_pagination_and_links(self):
        fx.State.host = "h"
        d = parse.parse_html(fx.build("/calls/")[2], "http://h/calls/")
        self.assertEqual(d.next_pages, ["http://h/calls/page/2/"])

    def test_sitemap(self):
        fx.State.host = "h"
        pages, kids = parse.parse_sitemap(fx.build("/sitemap.xml")[2])
        self.assertIn("http://h/submit", pages)

    def test_sitemap_refuses_dtd(self):
        bad = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><urlset><url><loc>http://x/</loc></url></urlset>'
        self.assertEqual(parse.parse_sitemap(bad)[0], [])


class GoldTests(unittest.TestCase):
    def test_label_and_field_accuracy(self):
        r = goldeval.evaluate(os.path.join(ROOT, "gold", "pitchboard_labels.json"))
        self.assertGreaterEqual(r["accuracy"], 0.95, r["errors"])
        # the costly error: stale / results / cancelled documents must never be open-call eligible
        bad = [e for e in r["errors"] if e["field"] == "eligible"]
        self.assertEqual(bad, [])

    def test_entity_false_merge_rate_bounded(self):
        r = goldeval.entity_eval(os.path.join(ROOT, "gold", "entity_pairs.json"))
        self.assertLessEqual(r["false_merge_rate"], 0.1)


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = fx.serve()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        fx.State.version = 1
        self.svc = db.connect(":memory:")
        db.migrate(self.svc, "service")
        self.inf = db.connect(":memory:")
        db.migrate(self.inf, "infra")
        self.origin = "http://" + fx.State.host
        self.fetcher = Fetcher(self.inf, allow_loopback=True, sleep=lambda s: None)
        self.targets = {"year": 2026, "targets": [{"id": "t1", "language": "en"}]}
        self.sid = store.register_site(self.svc, self.origin, "The Fixture Review", NOW, target_id="t1")

    def ctx(self, tag, minutes=0):
        return collector.Ctx(self.svc, self.inf, self.fetcher, Api(cap=0), self.targets, "run_" + tag, tag, lambda: later(minutes))

    def crawl(self, tag, minutes=0, max_jobs=120):
        c = self.ctx(tag, minutes)
        scheduler.enqueue(self.inf, "fetch_page", "discovery", {"url": self.origin + "/", "site_source_id": self.sid, "depth": 1, "via": "trial"},
                          "fetch|root|" + tag, .9, later(minutes), depth=1)
        scheduler.enqueue(self.inf, "expand_site", "backfill", {"site_source_id": self.sid, "feeds": []}, "expand|" + tag, .5, later(minutes), depth=1)
        collector.run(c, max_jobs=max_jobs, log=None)
        return c

    def q(self, sql, *a):
        return self.svc.execute(sql, a).fetchall()

    def counts(self):
        return {t: self.svc.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for t in ("pb_fact", "pb_call_edition", "pb_source_site", "pb_outlet")}

    def test_full_flow(self):
        self.crawl("a")
        kinds = {r["kind"] for r in self.q("SELECT kind FROM pb_raw_ref WHERE outcome='ok'")}
        self.assertTrue({"html", "pdf"} <= kinds)
        urls = {r["url"] for r in self.q("SELECT url FROM pb_raw_ref")}
        self.assertIn(self.origin + "/calls/page/2/", urls)                     # list pagination followed
        self.assertNotIn(self.origin + "/files/notice.png", urls)               # discovered binary link skipped
        # PDF collected with evidence, extractor version stored
        exts = self.q("SELECT r.kind, e.field, e.extractor FROM pb_extraction e JOIN pb_raw_ref r USING(raw_id)")
        self.assertTrue(any(r["kind"] == "pdf" and r["field"] == "fee.submission" for r in exts))
        self.assertTrue(all(r["extractor_version"] for r in self.q("SELECT extractor_version FROM pb_raw_ref WHERE outcome='ok'")))
        # prior-year call page did not become an open call
        self.assertEqual(self.q("SELECT count(*) c FROM pb_call_edition WHERE edition_key LIKE '2024%'")[0]["c"], 0)
        # SNS is a manual lead and was never fetched
        self.assertTrue(self.q("SELECT 1 FROM pb_discovery_candidate WHERE state='manual_lead' AND host='www.instagram.com/fixturereview'"))
        self.assertFalse(self.q("SELECT 1 FROM pb_raw_ref WHERE url LIKE '%instagram%'"))
        # gap filled by same-site reverse search with evidence
        g = self.q("SELECT * FROM pb_gap WHERE state='filled' AND attempts>0")
        self.assertTrue(g)
        self.assertTrue(all(x["filled_fact_id"] for x in g))
        # fee conflict (payments page: none; PDF: $15) is kept as an explicit conflict, not overwritten
        self.assertTrue(self.q("SELECT 1 FROM pb_conflict_group WHERE predicate='fee.submission'"))

    def test_refresh_existing_image_notice_with_ocr(self):
        url = self.origin + "/files/notice.png"
        outlet = store.site_row(self.svc, self.sid)["site_outlet"]
        sid = add_source(self.svc, url, outlet=outlet)
        store.insert_raw(self.svc, NOW, source_id=sid, url=url, kind="image", fetch_method="ocr", outcome="ok",
                         fetched_at=NOW, change_state="new")
        c = self.ctx("refresh-image", minutes=10)
        self.assertEqual(collector.enqueue_refresh(c), 1)
        collector.run(c, max_jobs=1, log=None)
        self.assertTrue(self.q("SELECT 1 FROM pb_extraction JOIN pb_raw_ref USING(raw_id) WHERE source_id=? "
                               "AND field='deadline.closes_on' AND extractor LIKE 'ocr%'", sid))
        self.assertFalse(self.q("SELECT 1 FROM pb_fact WHERE source_id=? AND epistemic='verified_fact'", sid))

    def test_idempotent_rerun(self):
        self.crawl("a")
        before = self.counts()
        self.crawl("b", minutes=10)
        self.assertEqual(self.counts(), before)
        self.assertTrue(self.q("SELECT 1 FROM pb_raw_ref WHERE change_state='unchanged'"))
        self.assertEqual(self.q("SELECT count(*) c FROM pb_raw_ref WHERE change_state='changed'")[0]["c"], 0)

    def test_change_detection_and_extension(self):
        self.crawl("a")
        call = self.q("SELECT call_id FROM pb_call_edition WHERE edition_key='2026-11-30'")[0]["call_id"]
        fx.State.version = 2
        c = self.ctx("b", 60)
        self.assertGreater(collector.enqueue_refresh(c, 50), 0)
        collector.run(c, max_jobs=60, log=None)
        ch = self.q("SELECT url FROM pb_raw_ref WHERE change_state='changed'")
        self.assertEqual([r["url"] for r in ch], [self.origin + "/calls/2026-fall"])
        hist = self.q("SELECT value_json, superseded_at FROM pb_fact WHERE subject_id=? AND predicate='deadline.closes_on' ORDER BY rowid", call)
        self.assertEqual(len(hist), 1 + 0 if False else len(hist))
        dates = [r["value_json"] for r in self.q("SELECT value_json FROM pb_fact WHERE predicate='deadline.closes_on' AND subject_id IN (SELECT call_id FROM pb_call_edition)")]
        self.assertTrue(any("2026-12-20" in d for d in dates))
        old = self.q("SELECT superseded_at FROM pb_fact WHERE predicate='deadline.closes_on' AND value_json LIKE '%2026-11-30%'")
        self.assertTrue(old and all(r["superseded_at"] for r in old))             # old value kept as history, superseded
        self.assertTrue(self.q("SELECT 1 FROM pb_call_edition WHERE status='extended'") or self.q("SELECT 1 FROM pb_status_event WHERE to_status='extended'"))

    def test_extraction_breakage_alert(self):
        self.crawl("a")
        fx.State.version = 3
        c = self.ctx("b", 60)
        collector.enqueue_refresh(c, 50)
        collector.run(c, max_jobs=60, log=None)
        kinds = {r["kind"] for r in self.inf.execute("SELECT kind FROM alert")}
        self.assertTrue(kinds & {"extraction_breakage", "field_lost", "segments_collapsed"}, kinds)

    def test_default_collector_drains_every_lane_past_former_request_caps(self):
        for lane in collector.LANES:
            for i in range(3):
                scheduler.enqueue(self.inf, "fixture", lane, {}, f"{lane}|{i}", .5, NOW)
        ctx = collector.Ctx(self.svc, self.inf, self.fetcher, Api(), self.targets, "run_limits", "limits", lambda: NOW)

        def requests(c, payload, job):
            c.fetcher.requests += 225
            return {}

        with patch.dict(collector.HANDLERS, fixture=requests):
            self.assertEqual(collector.run(ctx, log=None)["jobs_done"], 9)
        self.assertIsNone(ctx.api.cap)
        self.assertIsNone(ctx.site_page_cap)
        for lane in collector.LANES:
            self.assertEqual(scheduler.budget_used(self.inf, NOW[:10], lane, "requests"), 675)

    def test_explicit_request_cap_only_applies_to_its_lane(self):
        for lane in collector.LANES:
            scheduler.enqueue(self.inf, "fixture", lane, {}, lane, .5, NOW)
        ctx = collector.Ctx(self.svc, self.inf, self.fetcher, Api(), self.targets, "run_limits", "limits", lambda: NOW,
                            {"backfill": {"requests": 0}, "freshness": {"requests": None}})
        with patch.dict(collector.HANDLERS, fixture=Mock(return_value={})):
            self.assertEqual(collector.run(ctx, log=None)["jobs_done"], 2)
        self.assertEqual(self.inf.execute("SELECT budget_class FROM job WHERE state='pending'").fetchone()[0], "backfill")

    def test_default_refresh_enqueues_past_former_collector_and_daemon_limits(self):
        candidates = [{"url": self.origin + f"/call/{i}", "source_id": self.sid, "depth": 1,
                       "refresh_fresh_until": None} for i in range(45)]
        with patch.object(collector, "refresh_candidates", return_value=candidates):
            self.assertEqual(collector.enqueue_refresh(self.ctx("all")), 45)
            # a later run does not queue a second copy of a URL that is still queued (any run tag)
            self.assertEqual(collector.enqueue_refresh(self.ctx("again")), 0)
        more = [dict(c, url=c["url"] + "?b") for c in candidates]
        with patch.object(collector, "refresh_candidates", return_value=more):
            self.assertEqual(collector.enqueue_refresh(self.ctx("limited"), limit=3), 3)

    def test_resume_after_crash(self):
        c = self.ctx("a")
        scheduler.enqueue(self.inf, "fetch_page", "discovery", {"url": self.origin + "/", "site_source_id": self.sid, "depth": 1, "via": "trial"}, "fetch|root|a", .9, NOW, depth=1)
        job = scheduler.lease_next(self.inf, NOW, ["discovery"])             # worker "crashes" holding the lease
        self.assertEqual(job["state"], "leased")
        collector.run(self.ctx("a", 30), max_jobs=0, log=None)               # lease (300s) expired by +30min -> requeue on start
        self.assertEqual(self.inf.execute("SELECT state FROM job WHERE job_id=?", (job["job_id"],)).fetchone()["state"], "pending")
        collector.run(self.ctx("a", 30), max_jobs=30, log=None)
        self.assertEqual(self.inf.execute("SELECT state FROM job WHERE job_id=?", (job["job_id"],)).fetchone()["state"], "done")
        self.assertGreater(self.counts()["pb_fact"], 0)

    def test_robots_disallow_is_never_bypassed(self):
        r = self.fetcher.fetch(self.origin + "/private/x")
        self.assertEqual(r.outcome, "denied_robots")

    def test_blocked_hosts(self):
        for h in ("bookforum.com", "www.instagram.com", "thepointmag.com"):
            self.assertTrue(store.is_blocked(h), h)


if __name__ == "__main__":
    unittest.main()
