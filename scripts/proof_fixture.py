"""Fixture-based proof run (loopback test site, NOT a real outlet): multi-page list + PDF + image, gap fill,
idempotent rerun, change detection (deadline extension), extraction breakage, crash/resume.
Usage: PYTHONPATH=src:tests python scripts/proof_fixture.py OUT_DIR   -> prints JSON."""
import datetime as dt, json, os, sys
import fixture_site as fx
from pbdata import db
from pbdata.collect import collector, scheduler, store
from pbdata.collect.net import Fetcher
from pbdata.collect.wiki import Api

T0 = dt.datetime(2026, 10, 27, 6, 0, 0)
def at(m): return (T0 + dt.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")

def main(out):
    os.makedirs(out, exist_ok=True)
    srv = fx.serve(); origin = "http://" + fx.State.host; fx.State.version = 1
    sp, ip = os.path.join(out, "svc.sqlite3"), os.path.join(out, "infra.sqlite3")
    for p in (sp, ip):
        if os.path.exists(p): os.remove(p)
    svc = db.connect(sp); db.migrate(svc, "service"); inf = db.connect(ip); db.migrate(inf, "infra")
    fetcher = Fetcher(inf, allow_loopback=True, sleep=lambda s: None)
    targets = {"year": 2026, "targets": [{"id": "t1", "language": "en"}]}
    sid = store.register_site(svc, origin, "The Fixture Review", at(0), target_id="t1")
    q = lambda s, *a: svc.execute(s, a).fetchall()
    def counts(): return {t: svc.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for t in ("pb_fact", "pb_call_edition", "pb_source_site", "pb_outlet", "pb_conflict_group")}
    def crawl(tag, m, v, refresh=False, max_jobs=150):
        fx.State.version = v
        c = collector.Ctx(svc, inf, fetcher, Api(cap=0), targets, "run_" + tag, tag, lambda: at(m))
        if refresh: collector.enqueue_refresh(c, 80)
        else:
            scheduler.enqueue(inf, "fetch_page", "discovery", {"url": origin + "/", "site_source_id": sid, "depth": 1, "via": "trial"}, "fetch|root|" + tag, .9, at(m), depth=1)
            scheduler.enqueue(inf, "expand_site", "backfill", {"site_source_id": sid, "feeds": []}, "expand|" + tag, .5, at(m), depth=1)
        collector.run(c, max_jobs=max_jobs, log=None)
    R = {}
    crawl("a", 0, 1); R["run_a_counts"] = counts()
    R["kinds_collected"] = sorted({r["kind"] for r in q("SELECT kind FROM pb_raw_ref WHERE outcome='ok'")})
    R["list_page_2_followed"] = bool(q("SELECT 1 FROM pb_raw_ref WHERE url LIKE '%/calls/page/2/'"))
    R["image_ocr_deadline"] = [dict(r) for r in q("SELECT e.field, e.value_json v, e.confidence FROM pb_extraction e JOIN pb_raw_ref r USING(raw_id) WHERE r.kind='image' AND e.field='deadline.closes_on'")]
    R["gaps_filled_with_evidence"] = q("SELECT count(*) c FROM pb_gap WHERE state='filled' AND filled_fact_id IS NOT NULL")[0]["c"]
    R["gap_states"] = {r["state"]: r["c"] for r in q("SELECT state, count(*) c FROM pb_gap GROUP BY state")}
    before = counts(); crawl("b", 10, 1)
    R["rerun_counts_equal"] = counts() == before
    R["rerun_unchanged_raw"] = q("SELECT count(*) c FROM pb_raw_ref WHERE change_state='unchanged'")[0]["c"]
    crawl("c", 60, 2, refresh=True)
    R["changed_urls_v2"] = [r["url"].replace(origin, "") for r in q("SELECT url FROM pb_raw_ref WHERE change_state='changed'")]
    R["deadline_history"] = [dict(r) for r in q("SELECT value_json v, superseded_at s FROM pb_fact WHERE predicate='deadline.closes_on' ORDER BY rowid")]
    R["status_events"] = [dict(r) for r in q("SELECT from_status, to_status FROM pb_status_event ORDER BY rowid")]
    crawl("d", 120, 3, refresh=True)
    R["alerts_v3"] = sorted({r["kind"] for r in inf.execute("SELECT kind FROM alert")})
    srv.shutdown()
    print(json.dumps(R, indent=1, default=str))

if __name__ == "__main__":
    main(sys.argv[1])
