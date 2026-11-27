"""A sitemap-walking job (gap_fill / expand_site) gets its own wall-clock slice (2026-11-27): one gap_fill walked every
georgetown.edu subdomain sitemap for 1-2 h per drain chunk because the run budget was only checked between jobs."""
import os
import unittest
from unittest.mock import patch

from pbdata.collect import collector, scheduler
from test_queue_fairness import Base, NOW


class FakeRes:
    def __init__(self, body):
        self.outcome, self.body = "ok", body


class SitemapFetcher:
    """Each sitemap file costs 100 s on the fake clock and lists two child sitemaps (an endless index)."""
    requests = 0

    def __init__(self, clock):
        self.clock, self.fetched = clock, []

    def robots_sitemaps(self, origin):
        return [origin + "/sitemap.xml"]

    def fetch(self, url, **k):
        self.clock[0] += 100
        self.fetched.append(url)
        n = len(self.fetched)
        body = ("<sitemapindex><sitemap><loc>https://www.example.edu/sm%da.xml</loc></sitemap>"
                "<sitemap><loc>https://www.example.edu/sm%db.xml</loc></sitemap>"
                "<url><loc>https://www.example.edu/p%d</loc></url></sitemapindex>" % (n, n, n)).encode()
        return FakeRes(body)


class SiteJobSlice(Base):
    def test_sitemap_walk_stops_at_job_slice(self):
        clock = [1000.0]
        f = SitemapFetcher(clock)
        ctx = self.ctx()
        ctx.fetcher = f
        seen = {}

        def gap(c, payload, job):
            seen["deadline"] = c.job_deadline
            seen["urls"] = collector._sitemap_urls(c, "https://www.example.edu")
            return {"state": "ok"}
        scheduler.enqueue(self.infra, "gap_fill", "backfill", {"gap_id": "g", "site_source_id": "s"}, "gap|g", 0.9, NOW)
        with patch.dict(collector.HANDLERS, {"gap_fill": gap}), patch.dict(os.environ, {"PB_SITE_JOB_MAX_S": "600"}):
            collector.run(ctx, log=None, time_budget_s=7200, monotonic=lambda: clock[0])
        self.assertEqual(1600.0, seen["deadline"])
        self.assertEqual(6, len(f.fetched))           # 1000 -> 1600 at 100 s per sitemap file
        self.assertEqual(1, ctx.stats.get("sitemap_walk_time_up"))
        self.assertIsNone(ctx.job_deadline)          # cleared after the job

    def test_run_deadline_caps_the_slice(self):
        clock = [1000.0]
        seen = []

        def gap(c, payload, job):
            seen.append(c.job_deadline)
            return {"state": "ok"}
        scheduler.enqueue(self.infra, "gap_fill", "backfill", {"gap_id": "g", "site_source_id": "s"}, "gap|g", 0.9, NOW)
        with patch.dict(collector.HANDLERS, {"gap_fill": gap}), patch.dict(os.environ, {"PB_SITE_JOB_MAX_S": "600"}):
            collector.run(self.ctx(), log=None, time_budget_s=300, monotonic=lambda: clock[0])
        self.assertEqual([1300.0], seen)

    def test_zero_disables_slice_but_keeps_run_deadline(self):
        clock = [1000.0]
        seen = []

        def gap(c, payload, job):
            seen.append(c.job_deadline)
            return {"state": "ok"}
        scheduler.enqueue(self.infra, "gap_fill", "backfill", {"gap_id": "g", "site_source_id": "s"}, "gap|g", 0.9, NOW)
        with patch.dict(collector.HANDLERS, {"gap_fill": gap}), patch.dict(os.environ, {"PB_SITE_JOB_MAX_S": "0"}):
            collector.run(self.ctx(), log=None, time_budget_s=7200, monotonic=lambda: clock[0])
        self.assertEqual([8200.0], seen)


if __name__ == "__main__":
    unittest.main()
