"""Active collector: job handlers + run loop wiring discoverer, evaluator, fetch executor, promotion, gap finder and scheduler."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import sqlite3
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .. import facts, timeutil
from ..db import transaction
from ..profiles import pitchboard as prof
from . import discover, evaluate, executor, gaps, names, parse, promote, resolve, scheduler, store
from .net import Fetcher, FetchResult, normalize_url, origin_of, page_url, registered_domain
from .wiki import Api

LANES = ("discovery", "backfill", "freshness")


class Ctx:
    def __init__(self, svc, infra, fetcher: Fetcher, api: Api, targets: dict, run_id: str, run_tag: str, clock: Callable[[], str],
                 caps: Optional[dict] = None, *, api_cap: Optional[int] = None):
        self.svc, self.infra, self.fetcher, self.api = svc, infra, fetcher, api
        self.targets = {t["id"]: t for t in targets["targets"]}
        self.year = targets.get("year") or int(clock()[:4])
        self.run_id, self.run_tag, self.clock = run_id, run_tag, clock
        self.request_caps = {lane: (limits or {}).get("requests") for lane, limits in (caps or {}).items()}
        self.trial_cache: Dict[str, FetchResult] = {}
        self.limits = targets.get("budget") or {}
        if api_cap is not None:
            self.api.cap = api_cap
        elif self.limits.get("api_calls") is not None and getattr(self.api, "cap", None) is None:
            self.api.cap = self.limits.get("api_calls")
        self.budget = discover.Budget(max_candidates=self.limits.get("max_candidates"),
                                      max_depth=self.limits.get("max_depth"))
        site_page_cap = os.environ.get("PB_MAX_PAGES_PER_SITE")
        self.site_page_cap = int(site_page_cap) if site_page_cap is not None else self.limits.get("max_pages_per_site")
        self.sitemap_cache: Dict[Tuple[str, Optional[int]], List[str]] = {}
        self.stats: Dict[str, int] = {}
        self.touched = set()

    def bump(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n


def _day(now: str) -> str:
    return now[:10]


def seed_discovery(ctx: Ctx) -> int:
    now, n = ctx.clock(), 0
    queued = scheduler.queued_keys(ctx.infra, "discover")   # one queued copy per target+strategy, whatever run seeded it
    for tid, t in ctx.targets.items():
        for strat in ("call_index", "wikidata_class", "wiki_category", "query_combo"):
            if strat == "call_index" and not t.get("call_indexes"):
                continue
            if scheduler.dedupe_key("discover", {"target_id": tid, "strategy": strat}) in queued:
                continue
            if scheduler.enqueue(ctx.infra, "discover", "discovery", {"target_id": tid, "strategy": strat}, "discover|%s|%s|%s" % (tid, strat, ctx.run_tag),
                                 scheduler.priority(novelty=1.0, quality=0.6, cost=0.2), now):
                n += 1
    return n


# ------------------------------------------------------------------ handlers
def h_discover(ctx: Ctx, p: dict, job) -> dict:
    t, now = ctx.targets[p["target_id"]], ctx.clock()
    strat = p["strategy"]
    if strat == "wikidata_class":
        cands = discover.wikidata_class(ctx.api, t, ctx.limits.get("wikidata_results"))
    elif strat == "wiki_category":
        cands = discover.wiki_category(ctx.api, t, ctx.limits.get("category_members"))
    elif strat == "query_combo":
        cands = discover.query_search(ctx.api, t, ctx.year, ctx.limits.get("max_queries"),
                                      results=ctx.limits.get("search_results"))
    elif strat == "call_index":
        cands = discover.call_index(ctx.fetcher, t, now, ctx.year, ctx.limits.get("call_index_pages"))
    elif strat == "entity_followup":
        cands = discover.entity_followup(ctx.api, p["name"], t.get("language", "en"), p.get("parent", ""),
                                          ctx.limits.get("entity_search_results"))
    else:
        raise ValueError("unknown strategy")
    ids = discover.add_candidates(ctx.svc, cands, t["id"], now, ctx.budget, depth=p.get("depth", 0))
    # Prior relevance sets the evaluation order.
    for cid in ids:
        prior = ctx.svc.execute("SELECT prior_relevance, host, label FROM pb_discovery_candidate WHERE cand_id=?", (cid,)).fetchone()
        dup = 1.0 if store.is_seed(prior["host"]) else 0.0
        lit = 0.25 if prof.LIT_EN.search(prior["host"] + " " + (prior["label"] or "")) or prof.LIT_KO.search(prior["label"] or "") else 0.0
        scheduler.enqueue(ctx.infra, "evaluate", "discovery", {"cand_id": cid}, "evaluate|%s|%s" % (cid, ctx.run_tag),
                          scheduler.priority(novelty=1.0 - dup, quality=min(1.0, prior["prior_relevance"] + lit), cost=0.4, duplication=dup), now, depth=p.get("depth", 0))
    ctx.bump("candidates_found", len(cands))
    ctx.bump("candidates_new", len(ids))
    return {"found": len(cands), "new": len(ids), "api_calls": ctx.api.calls}


def h_evaluate(ctx: Ctx, p: dict, job) -> dict:
    now = ctx.clock()
    cand = ctx.svc.execute("SELECT * FROM pb_discovery_candidate WHERE cand_id=?", (p["cand_id"],)).fetchone()
    if cand["state"] != "new":
        return {"skipped": cand["state"]}
    t = ctx.targets[cand["target_id"]]
    ev = evaluate.trial_crawl(ctx.svc, ctx.fetcher, cand, t, now, limit=ctx.limits.get("trial_pages"))
    source_id = None
    if ev.state == "registered":
        first = next(iter(ev.pages.values()))
        origin = origin_of(first.final_url)
        host = urlparse(origin).netloc.lower()
        alt = names.doc_names(next(iter(ev.docs.values())), host)
        label = names.pick_name([cand["label"], *alt], host)   # CTA/QID labels never become the canonical name
        dup = resolve.find_existing(ctx.svc, label, cand["host"])
        if dup:
            ev.state, ev.reason = "deprioritized", "duplicate_entity:" + dup
            ev.scores["duplicate"] = dup
    if ev.state == "registered":
        if store.is_blocked(host):
            ev.state, ev.reason = "rejected", "blocked_domain_after_redirect"
        else:
            source_id = store.register_site(ctx.svc, origin, label, now, target_id=t["id"], alt_names=alt,
                                            qid=(cand["origin_ref"].rsplit(":", 1)[-1] if cand["origin_ref"] and cand["origin_ref"].rsplit(":", 1)[-1].startswith("Q") else None))
    with transaction(ctx.svc):
        ctx.svc.execute("UPDATE pb_discovery_candidate SET state=?, reason=?, score_json=?, source_id=?, evaluated_at=? WHERE cand_id=?",
                        (ev.state, ev.reason, json.dumps(ev.scores, sort_keys=True), source_id, now, cand["cand_id"]))
    if source_id:
        for u, r in ev.pages.items():
            u = page_url(u)
            if r.outcome == "ok" and u is not None:
                ctx.trial_cache[u] = r
                scheduler.enqueue(ctx.infra, "fetch_page", "discovery", {"url": u, "site_source_id": source_id, "depth": 1, "via": "trial"},
                                  "fetch|%s|%s" % (u, ctx.run_tag), scheduler.priority(novelty=1.0, quality=0.7, urgency=0.5), now, depth=1)
        scheduler.enqueue(ctx.infra, "expand_site", "backfill", {"site_source_id": source_id, "feeds": ev.feeds}, "expand|%s|%s" % (source_id, ctx.run_tag),
                          scheduler.priority(novelty=0.9, quality=ev.scores.get("relevance", 0.5), cost=0.5), now, depth=1)
    ctx.bump("evaluated")
    ctx.bump("eval_" + ev.state)
    return {"state": ev.state, "reason": ev.reason, "requests": ev.requests, "relevance": ev.scores.get("relevance")}


def _site_page_count(ctx, site_source_id: str) -> int:
    site = store.site_row(ctx.svc, site_source_id)
    return ctx.svc.execute("SELECT count(DISTINCT r.url) FROM pb_raw_ref r JOIN pb_source s ON s.source_id=r.source_id WHERE s.owner_outlet_id=? AND r.run_id=?",
                           (site["site_outlet"], ctx.run_id)).fetchone()[0]


def h_fetch_page(ctx: Ctx, p: dict, job) -> dict:
    now = ctx.clock()
    url, site_id, depth = normalize_url(p["url"]), p["site_source_id"], p.get("depth", 1)
    if (ctx.site_page_cap is not None and p.get("via") not in ("trial", "refresh", "gap", "retry")
            and _site_page_count(ctx, site_id) >= ctx.site_page_cap):
        return {"skipped": "site_page_cap"}
    site = store.site_row(ctx.svc, site_id)
    ctx.touched.add((site["site_outlet"], site["target_id"]))
    if site["permission"] not in ("allowed_public", "allowed_conditional") or store.is_blocked(urlparse(url).netloc):
        return {"skipped": "permission"}
    pf = ctx.trial_cache.pop(url, None)
    r = executor.fetch_page(ctx.svc, ctx.fetcher, url, site_id, now, run_id=ctx.run_id, depth=depth, infra=ctx.infra, prefetched=pf,
                            force_reextract=bool(p.get("force_reextract")), refresh=p.get("via") == "refresh")
    if r.skip_reason:
        return {"skipped": r.skip_reason}
    out = {"outcome": r.outcome, "change": r.change_state, "kind": r.kind, "requests": 0 if pf else r.requests}
    ctx.bump("pages_" + r.outcome)
    if r.outcome != "ok":
        gaps.compute_gaps(ctx.svc, site["site_outlet"], site["target_id"], now)
        return out
    if r.change_state != "unchanged" or r.reextracted:
        ps = promote.promote_raw(ctx.svc, r.raw_id, now)
        out.update(facts=ps.facts, verified=ps.verified, calls=ps.calls_created, skipped_label=ps.skipped_label, rejected=ps.rejected)
        ctx.bump("facts", ps.facts)
        ctx.bump("verified_facts", ps.verified)
        if r.kind in ("pdf", "image"):
            ctx.bump("%s_collected" % r.kind)
        if r.ocr:
            ctx.bump("ocr_docs")
    gp = gaps.compute_gaps(ctx.svc, site["site_outlet"], site["target_id"], now)
    out["gaps_open"] = gp["open"]
    _enqueue_gap_jobs(ctx, site, now)
    crawl_depth = ctx.limits.get("crawl_depth")
    if r.doc is not None and r.kind == "html" and (crawl_depth is None or depth < crawl_depth):
        _follow(ctx, r, site, site_id, r.url, depth, now, p.get("hop", 0))
    return out


def _same_site(a: str, b: str) -> bool:
    return registered_domain(urlparse(a).netloc.lower()) == registered_domain(urlparse(b).netloc.lower())


def _follow(ctx: Ctx, r, site, site_id: str, url: str, depth: int, now: str, hop: int = 0) -> None:
    """Chain: list pagination, guidelines, document attachments, apply forms, SNS leads, entity follow-ups."""
    doc = r.doc
    n = 0
    seen_urls = {url}
    max_hops = ctx.limits.get("pagination_hops")
    for nxt in doc.next_pages[:ctx.limits.get("pagination_links")]:
        nxt = page_url(nxt)
        if nxt is not None and nxt not in seen_urls and _same_site(nxt, url) and (max_hops is None or hop < max_hops):
            scheduler.enqueue(ctx.infra, "fetch_page", "backfill", {"url": nxt, "site_source_id": site_id, "depth": depth, "via": "pagination", "hop": hop + 1},
                              "fetch|%s|%s" % (nxt, ctx.run_tag), scheduler.priority(novelty=0.6, quality=0.5, cost=0.3), now, depth=depth)
            seen_urls.add(nxt)
    max_links = ctx.limits.get("follow_links")
    for l in sorted(doc.links, key=lambda x: -prof.link_priority(x)):
        if max_links is not None and n >= max_links:
            break
        href = page_url(l.href)
        if href is None or href in seen_urls or not href.startswith("http") or prof.cycle_score(href + " " + (l.text or "")) < 0:
            continue
        host = urlparse(href).netloc.lower()
        if store.is_blocked(host):
            if prof.SNS_HOST.search(l.href):
                _sns_lead(ctx, l.href, site, now)
            continue
        pr = prof.link_priority(dataclasses.replace(l, href=href))
        attachment = prof.ATTACH_RE.search(href)
        if attachment and _same_site(href, url) and (pr > 0 or re.search(r"guideline|rules|요강|공고|공모|submission|notice|poster|announce|call|공지|포스터", href + l.text, re.I)):
            scheduler.enqueue(ctx.infra, "fetch_page", "backfill", {"url": href, "site_source_id": site_id, "depth": depth + 1, "via": "attachment"},
                              "fetch|%s|%s" % (href, ctx.run_tag), scheduler.priority(novelty=0.8, quality=0.8, urgency=0.5, cost=0.4), now, depth=depth + 1)
            n += 1
            seen_urls.add(href)
        elif pr >= 0.5 and _same_site(href, url) and not attachment and not re.search(r"/(20(0\d|1\d))/", href):
            scheduler.enqueue(ctx.infra, "fetch_page", "backfill", {"url": href, "site_source_id": site_id, "depth": depth + 1, "via": "link"},
                              "fetch|%s|%s" % (href, ctx.run_tag), scheduler.priority(novelty=0.7, quality=min(1, pr), cost=0.3), now, depth=depth + 1)
            n += 1
            seen_urls.add(href)
        elif prof.SNS_HOST.search(l.href):
            _sns_lead(ctx, l.href, site, now)
    # entity follow-ups from text (organisers / sponsors named on the page)
    followup_depth = ctx.limits.get("entity_followup_depth")
    if followup_depth is not None and depth > followup_depth:
        return
    entities = list(dict.fromkeys(e.value["name"] for e in r.exts if e.field == "signal.entity"))
    for name in entities[:ctx.limits.get("entity_followups")]:
        scheduler.enqueue(ctx.infra, "discover", "discovery", {"target_id": site["target_id"], "strategy": "entity_followup", "name": name, "parent": url, "depth": depth},
                          "discover|entity|%s|%s" % (name.lower(), ctx.run_tag), scheduler.priority(novelty=0.8, quality=0.4, cost=0.3), now, depth=depth)


def _sns_lead(ctx: Ctx, href: str, site, now: str) -> None:
    """Homepage -> SNS: recorded as a manual lead candidate (never fetched; no official API)."""
    pu = urlparse(href)
    first = pu.path.strip("/").split("/")[0] if pu.path.strip("/") else ""
    if not first:
        return
    host = pu.netloc.lower() + "/" + first
    from ..ids import new_id
    ctx.svc.execute("INSERT OR IGNORE INTO pb_discovery_candidate(cand_id,url,host,label,target_id,strategy,origin_ref,depth,prior_relevance,state,reason,discovered_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (new_id("cand"), href, host, (site["owner_name"] or "")[:120], site["target_id"], "sns_link", site["source_id"], 1, 0.3,
                                                         "manual_lead", "sns_not_auto_collected", now))


def _enqueue_gap_jobs(ctx: Ctx, site, now: str) -> None:
    max_attempts = ctx.limits.get("gap_attempts")
    for g in ctx.svc.execute("SELECT * FROM pb_gap WHERE state='open' AND gap_class IN ('not_extracted','absent_in_source') "
                             "AND (? IS NULL OR attempts<?) AND "
                             "subject_id IN (SELECT ? UNION SELECT call_id FROM pb_call_edition WHERE outlet_id=?)",
                             (max_attempts, max_attempts, site["site_outlet"], site["site_outlet"])):
        scheduler.enqueue(ctx.infra, "gap_fill", "backfill", {"gap_id": g["gap_id"], "site_source_id": site["source_id"]},
                          "gap|%s|%d|%s" % (g["gap_id"], g["attempts"], ctx.run_tag), scheduler.priority(novelty=0.5, gap_importance=g["importance"], quality=0.6, cost=0.3), now)


def _sitemap_urls(ctx: Ctx, origin: str, limit: Optional[int] = None) -> List[str]:
    if limit is None:
        limit = ctx.limits.get("sitemap_urls")
    cache_key = (origin, limit)
    if cache_key in ctx.sitemap_cache:
        return ctx.sitemap_cache[cache_key].copy()
    urls: List[str] = []
    cands = ctx.fetcher.robots_sitemaps(origin) or [origin + "/sitemap.xml"]
    queue, seen = deque(cands), set()
    max_files = ctx.limits.get("sitemap_files")
    while queue and (max_files is None or len(seen) < max_files) and (limit is None or len(urls) < limit):
        sm = queue.popleft()
        if sm in seen or not _same_site(sm, origin):
            continue
        seen.add(sm)
        res = ctx.fetcher.fetch(sm, accept="application/xml,text/xml", max_bytes=6_000_000, now_iso=ctx.clock())
        if res.outcome != "ok":
            continue
        pages, kids = parse.parse_sitemap(res.body)
        urls += pages
        queue.extend(sorted(kids, key=lambda k: not re.search(r"post|page|news|notice|board|call|submission", k, re.I)))
    ctx.sitemap_cache[cache_key] = urls[:limit]
    return ctx.sitemap_cache[cache_key].copy()


def h_expand_site(ctx: Ctx, p: dict, job) -> dict:
    now = ctx.clock()
    site = store.site_row(ctx.svc, p["site_source_id"])
    origin = site["origin"]
    urls = _sitemap_urls(ctx, origin)
    for f in p.get("feeds", [])[:ctx.limits.get("feeds")]:
        res = ctx.fetcher.fetch(f, accept="application/rss+xml,application/atom+xml,application/xml", now_iso=now)
        if res.outcome == "ok":
            urls += [it["link"] for it in parse.parse_feed(res.body)[:ctx.limits.get("feed_items")] if it["link"]]
    picked = []
    for u in dict.fromkeys(map(page_url, urls)):
        if u is None or not (prof.HIGH_VALUE_PATH.search(u) or (prof.NEWS_PATH.search(u) and prof.cycle_score(u) > 0)) or prof.cycle_score(u) < 0 \
                or re.search(r"/tag/|/category/|/author/", u):
            continue
        picked.append(u)
    picked = sorted(picked, key=lambda u: -prof.cycle_score(u))[:ctx.limits.get("expansion_pages")]
    for u in picked:
        scheduler.enqueue(ctx.infra, "fetch_page", "backfill", {"url": u, "site_source_id": p["site_source_id"], "depth": 2, "via": "sitemap"},
                          "fetch|%s|%s" % (u, ctx.run_tag), scheduler.priority(novelty=0.8, quality=0.6, urgency=0.4, cost=0.3), now, depth=2)
    return {"sitemap_urls": len(urls), "enqueued": len(picked), "requests": 0}


def h_gap_fill(ctx: Ctx, p: dict, job) -> dict:
    """Reverse search for an empty field inside the same site: rank unfetched site URLs by field hint words, fetch, re-extract."""
    now = ctx.clock()
    g = ctx.svc.execute("SELECT * FROM pb_gap WHERE gap_id=?", (p["gap_id"],)).fetchone()
    if g["state"] != "open":
        return {"skipped": g["state"]}
    max_attempts = ctx.limits.get("gap_attempts")
    if max_attempts is not None and g["attempts"] >= max_attempts:
        return {"skipped": "gap_attempt_limit"}
    site = store.site_row(ctx.svc, p["site_source_id"])
    ctx.touched.add((site["site_outlet"], site["target_id"]))
    hints = [h.lower() for h in prof.FIELD_HINTS.get(g["predicate"], [])]
    urls = _sitemap_urls(ctx, site["origin"])
    fetched = {r[0] for r in ctx.svc.execute("SELECT DISTINCT url FROM pb_raw_ref r JOIN pb_source s USING(source_id) WHERE s.owner_outlet_id=?", (site["site_outlet"],))}
    scored = sorted(((sum(1 for h in hints if h in u.lower()), u) for u in dict.fromkeys(map(page_url, urls))
                     if u is not None and u not in fetched and not re.search(r"/(19|20(0\d|1\d))/|/tag/|/category/", u)), reverse=True)
    pick = [u for s, u in scored if s > 0][:ctx.limits.get("gap_pages")]
    for u in pick:
        r = executor.fetch_page(ctx.svc, ctx.fetcher, u, p["site_source_id"], now, run_id=ctx.run_id, depth=3, infra=ctx.infra)
        if r.outcome == "ok" and r.change_state != "unchanged":
            promote.promote_raw(ctx.svc, r.raw_id, now)
    with transaction(ctx.svc):
        ctx.svc.execute("UPDATE pb_gap SET attempts=attempts+1, last_attempt_at=? WHERE gap_id=?", (now, g["gap_id"]))
    gaps.compute_gaps(ctx.svc, site["site_outlet"], site["target_id"], now)
    st = ctx.svc.execute("SELECT state FROM pb_gap WHERE gap_id=?", (g["gap_id"],)).fetchone()["state"]
    ctx.bump("gap_fill_attempts")
    if st == "filled":
        ctx.bump("gaps_filled_by_search")
    return {"tried": pick, "state": st, "requests": len(pick)}


HANDLERS = {"discover": h_discover, "evaluate": h_evaluate, "fetch_page": h_fetch_page, "expand_site": h_expand_site, "gap_fill": h_gap_fill}


# ------------------------------------------------------------------ freshness
def refresh_candidates(svc: sqlite3.Connection, now: str, horizon_hours: float = 48) -> List[dict]:
    """Latest ok raw per URL, live-call expiry first, then oldest fetch (no infra needed).

    refresh_fresh_until is the soonest effective expiry requiring a refresh, or None
    for ordinary round-robin candidates. Prefer a fact source's canonical URL;
    when it has no ok raw, use that source's other ok raw URLs.

    A live-call source whose latest content was labeled only by an older labeler counts as
    expired now (refresh_fresh_until = now, label_stale = True): bodies are not kept, so a
    refetch is the only way a labeler fix reaches calls that are already shown as open.
    """
    if horizon_hours < 0:
        raise ValueError("refresh horizon must be nonnegative")
    horizon = timeutil.iso(timeutil.parse_iso(now) + dt.timedelta(hours=horizon_hours))
    rows = [dict(r, refresh_fresh_until=None, label_stale=False) for r in svc.execute(
        "SELECT url, source_id, depth, fetched_at AS last, text_hash FROM ("
        "SELECT r.*, row_number() OVER (PARTITION BY r.url ORDER BY r.fetched_at DESC, r.raw_id DESC) AS rn "
        "FROM pb_raw_ref r JOIN pb_source s USING(source_id) WHERE s.retired_reason IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM pb_source retired WHERE retired.canonical_url=r.url AND retired.retired_reason IS NOT NULL) "
        "AND r.outcome='ok' AND r.kind IN ('html','pdf','image')) "
        "WHERE rn=1 ORDER BY last, url")]
    by_source = {}
    for r in rows:
        by_source.setdefault(r["source_id"], []).append(r)
    live = svc.execute(
        "SELECT f.fact_id, f.source_id, s.canonical_url FROM pb_fact f "
        "JOIN pb_call_edition c ON f.subject_type='call' AND c.call_id=f.subject_id "
        "JOIN pb_source s ON s.source_id=f.source_id "
        "WHERE s.retired_reason IS NULL AND f.superseded_at IS NULL AND c.status IN ('announced','open','extended','standing') "
        "AND (f.predicate='status.declared' OR (f.predicate='deadline.closes_on' "
        "AND json_extract(f.value_json,'$.date')>=?))", (now[:10],)).fetchall()
    for f in live:
        source_rows = by_source.get(f["source_id"], [])
        preferred = [r for r in source_rows if r["url"] == f["canonical_url"]] or source_rows
        fresh_until = facts.effective_fresh_until(svc, f["fact_id"])
        for r in preferred:
            due = fresh_until if fresh_until is not None and fresh_until <= horizon else None
            if executor.labels_stale(svc, r["source_id"], r["text_hash"]):
                r["label_stale"] = True
                due = now if due is None else min(due, now)
            if due is not None and (r["refresh_fresh_until"] is None or due < r["refresh_fresh_until"]):
                r["refresh_fresh_until"] = due
    for r in rows:
        del r["text_hash"]
    return sorted(rows, key=lambda r: (0, r["refresh_fresh_until"], r["url"])
                  if r["refresh_fresh_until"] is not None else (1, r["last"], r["url"]))


def enqueue_refresh(ctx: Ctx, limit: Optional[int] = None, horizon_hours: Optional[float] = None) -> int:
    if limit is None:
        limit = ctx.limits.get("refresh_limit")
    if limit is not None and limit <= 0:
        return 0
    if horizon_hours is None:
        horizon_hours = float(os.environ.get("PB_REFRESH_HORIZON_HOURS", "48"))
    now = ctx.clock()
    n = 0
    # Each run used to enqueue every URL again under its own run tag, so the queue held one copy per run
    # (18,663 jobs for 3,714 URLs on 2026-11-26). A URL already queued is skipped; a higher priority is carried over.
    queued = scheduler.queued_keys(ctx.infra, "fetch_page", "freshness")
    for r in refresh_candidates(ctx.svc, now, horizon_hours):
        owner = ctx.svc.execute("SELECT ss.source_id FROM pb_source s JOIN pb_source_site ss ON ss.outlet_id=s.owner_outlet_id WHERE s.source_id=? ORDER BY ss.source_id LIMIT 1", (r["source_id"],)).fetchone()
        if not owner:
            continue
        closes = ctx.svc.execute("SELECT min(json_extract(value_json,'$.date')) d FROM pb_fact WHERE predicate='deadline.closes_on' AND superseded_at IS NULL AND subject_id IN "
                                 "(SELECT call_id FROM pb_call_edition WHERE outlet_id=(SELECT owner_outlet_id FROM pb_source WHERE source_id=?)) AND json_extract(value_json,'$.date')>=?", (r["source_id"], now[:10])).fetchone()["d"]
        if r["refresh_fresh_until"] is not None:
            # A live open call is due (fact expiry or older-labeler label): lease it before discovery/backfill work,
            # whose computed priorities (max 0.9) otherwise starve refresh jobs (max 0.285) for days.
            prio = scheduler.LIVE_CALL_REFRESH_PRIORITY
        else:
            prio = scheduler.priority(novelty=0.1, urgency=scheduler.urgency_from_deadline(closes, now[:10]), quality=0.5, cost=0.2)
        have = queued.get(scheduler.dedupe_key("fetch_page", {"url": r["url"], "via": "refresh"}))
        if have is not None:
            if prio > have[1]:
                ctx.infra.execute("UPDATE job SET priority=?, updated_at=? WHERE job_id=?", (prio, now, have[0]))
            continue
        if scheduler.enqueue(ctx.infra, "fetch_page", "freshness", {"url": r["url"], "site_source_id": owner["source_id"], "depth": r["depth"], "via": "refresh"},
                             "fetch|%s|%s" % (r["url"], ctx.run_tag), prio, now, depth=r["depth"]):
            n += 1
            if limit is not None and n >= limit:
                break
    return n


# A first fetch that failed for a network reason (proxy/DNS hiccup, reset, timeout, 5xx, robots.txt
# unreachable) was never tried again: refresh only revisits URLs with an ok raw, so the page stayed
# "never fetched" for good (2026-11-26: 361 of 464 never-fetched sources had one URLError from the
# 11-20..11-23 proxy outages; 12/12 sampled fetched ok on retry). Deterministic failures (robots
# disallow, 4xx, login walls, challenges, binary media, encoding bugs) are not retried.
RETRY_ERROR_CLASSES = ("URLError", "RemoteDisconnected", "robots_unreachable", "ConnectionResetError",
                       "ConnectionRefusedError", "ConnectionAbortedError", "TimeoutError", "OSError", "IncompleteRead")
RETRY_MAX_FAILURES = 3          # give up after this many transient failures (with no success) per URL
RETRY_BACKOFF_HOURS = 24        # wait 24h after the 1st failure, 48h after the 2nd
RETRY_PRIORITY = 0.4            # above discovery/backfill work (<= ~0.37 in practice), below live-call refresh (1.0)


def retry_candidates(svc: sqlite3.Connection, now: str) -> List[dict]:
    """Active sources never fetched ok whose failures were all transient, due by exponential backoff.

    One row per URL (the source's own failed URLs), oldest last attempt first."""
    rows = svc.execute(
        "SELECT r.url, r.source_id, max(r.depth) AS depth, count(*) AS failures, max(r.fetched_at) AS last, "
        "sum(CASE WHEN r.outcome IN ('timeout','server_error') OR (r.outcome='error' AND r.error_class IN (%s)) "
        "THEN 0 ELSE 1 END) AS permanent "
        "FROM pb_raw_ref r JOIN pb_source s USING(source_id) "
        "WHERE s.retired_reason IS NULL AND s.last_success_at IS NULL AND s.source_type<>'manual_clip' "
        "AND s.permission IN ('allowed_public','allowed_conditional') "
        "AND NOT EXISTS (SELECT 1 FROM pb_raw_ref ok WHERE ok.source_id=s.source_id AND ok.outcome='ok') "
        "GROUP BY r.url, r.source_id ORDER BY last, r.url" % ",".join("?" * len(RETRY_ERROR_CLASSES)),
        RETRY_ERROR_CLASSES).fetchall()
    t_now = timeutil.parse_iso(now)
    out = []
    for r in rows:
        if r["permanent"] or r["failures"] >= RETRY_MAX_FAILURES:
            continue
        due = timeutil.parse_iso(r["last"]) + dt.timedelta(hours=RETRY_BACKOFF_HOURS * 2 ** (r["failures"] - 1))
        if due <= t_now:
            out.append({"url": r["url"], "source_id": r["source_id"], "depth": r["depth"], "failures": r["failures"], "last": r["last"]})
    return out


def enqueue_retries(ctx: Ctx, limit: Optional[int] = None) -> int:
    """Queue a fresh attempt for transiently failed first fetches (see retry_candidates)."""
    if limit is None:
        limit = int(os.environ.get("PB_RETRY_FAILED_LIMIT", "150"))
    if limit <= 0:
        return 0
    now = ctx.clock()
    n = 0
    queued = scheduler.queued_keys(ctx.infra, "fetch_page", "freshness")
    for r in retry_candidates(ctx.svc, now):
        if scheduler.dedupe_key("fetch_page", {"url": r["url"], "via": "retry"}) in queued:
            continue
        owner = ctx.svc.execute("SELECT ss.source_id FROM pb_source s JOIN pb_source_site ss ON ss.outlet_id=s.owner_outlet_id "
                                "WHERE s.source_id=? ORDER BY ss.source_id LIMIT 1", (r["source_id"],)).fetchone()
        if not owner:
            continue
        if scheduler.enqueue(ctx.infra, "fetch_page", "freshness",
                             {"url": r["url"], "site_source_id": owner["source_id"], "depth": r["depth"], "via": "retry"},
                             "retry|%s|%d" % (r["url"], r["failures"]), RETRY_PRIORITY, now, depth=r["depth"]):
            n += 1
            if n >= limit:
                break
    return n


# ------------------------------------------------------------------ run loop
def run(ctx: Ctx, max_jobs: Optional[int] = None, log=print, time_budget_s: Optional[float] = None,
        monotonic: Callable[[], float] = time.time, *, lanes: Optional[Tuple[str, ...]] = None,
        stop_at: Optional[float] = None, freshness_every: Optional[int] = None) -> Dict[str, int]:
    """Lease and run jobs until the queue is empty, max_jobs is reached, or time_budget_s is spent.

    The budget is wall-clock time (time.time by default; the parameter keeps its old name for callers/tests):
    this box can be frozen for an hour or more, and a CLOCK_MONOTONIC budget does not advance while frozen, so a
    "90 minute" run stretched over 9 wall-clock hours (11-25 22:24 -> 11-26 07:34 KST) and held collector.lock
    across the 04:20 collector. stop_at is an absolute deadline on the same clock (the daemon passes the start of
    the daily window). The budget is checked before each lease, and Wikimedia API calls stop at the deadline, so a
    long discovery job ends with what it has; unleased jobs stay pending for the next run.

    lanes restricts which budget classes are leased (the daily refresh step leases freshness only). Every
    freshness_every-th lease prefers the freshness lane (default 2, PB_FRESHNESS_EVERY; 0 = pure priority order)."""
    deadline = monotonic() + time_budget_s if time_budget_s is not None else None
    if stop_at is not None:
        deadline = stop_at if deadline is None else min(deadline, stop_at)
    if freshness_every is None:
        freshness_every = int(os.environ.get("PB_FRESHNESS_EVERY", "2"))
    run_lanes = tuple(lanes) if lanes else LANES
    now = ctx.clock()
    scheduler.requeue_expired(ctx.infra, "9999-01-01T00:00:00Z" if os.environ.get("PB_RESUME_FORCE") else now)
    collapsed = scheduler.collapse_duplicates(ctx.infra, now)
    if collapsed:
        ctx.bump("duplicate_jobs_collapsed", collapsed)
    crash_after = int(os.environ.get("PB_CRASH_AFTER_LEASE", "0")) or None
    counters = {"done": 0, "leased": 0}
    ctx.infra.execute("INSERT OR IGNORE INTO runs(run_id,started_at) VALUES(?,?)", (ctx.run_id, now))
    prev_api_deadline = getattr(ctx.api, "deadline", None)
    if deadline is not None and hasattr(ctx.api, "deadline"):
        ctx.api.deadline, ctx.api.deadline_clock = deadline, monotonic
    try:
        _run_loop(ctx, max_jobs, log, time_budget_s, monotonic, deadline, run_lanes, freshness_every, crash_after, counters)
    finally:
        if hasattr(ctx.api, "deadline"):
            ctx.api.deadline = prev_api_deadline
    done = counters["done"]
    for outlet, tid in sorted(ctx.touched):         # settle: gap classification reflects the final state of every site touched in this run
        gaps.compute_gaps(ctx.svc, outlet, tid, ctx.clock())
    ctx.infra.execute("UPDATE runs SET finished_at=?, outcome=?, report=? WHERE run_id=?", (ctx.clock(), "ok", json.dumps(ctx.stats, sort_keys=True), ctx.run_id))
    ctx.stats["jobs_done"] = done
    return ctx.stats


def DISCOVER_JOB_MAX_S() -> float:
    """Wall-clock seconds of Wikimedia API calls one discover job may spend (PB_DISCOVER_JOB_MAX_S, default 600; 0 = no cap)."""
    return float(os.environ.get("PB_DISCOVER_JOB_MAX_S", "600"))


def _run_loop(ctx, max_jobs, log, time_budget_s, monotonic, deadline, run_lanes, freshness_every, crash_after, counters):
    while max_jobs is None or counters["done"] < max_jobs:
        done, leased = counters["done"], counters["leased"]
        if deadline is not None and monotonic() >= deadline:
            ctx.stats["stopped_time_budget"] = 1
            if log:
                log("[run] time budget of %ss spent after %d jobs; remaining jobs stay queued" % (time_budget_s, done))
            break
        now = ctx.clock()
        day = _day(now)
        allowed = [lane for lane in run_lanes if ctx.request_caps.get(lane) is None
                   or scheduler.budget_used(ctx.infra, day, lane, "requests") < ctx.request_caps[lane]]
        prefer = "freshness" if freshness_every and leased % freshness_every == 0 else None
        job = scheduler.lease_next(ctx.infra, now, allowed, prefer=prefer)
        if job is None:
            break
        leased += 1
        counters["leased"] = leased
        if crash_after and leased >= crash_after:
            os._exit(137)       # crash injection for the resume proof: job stays 'leased' with an unexpired lease
        payload = json.loads(job["payload"])
        before = ctx.fetcher.requests + ctx.api.calls
        run_api_deadline = getattr(ctx.api, "deadline", None)
        if job["kind"] == "discover" and hasattr(ctx.api, "deadline") and DISCOVER_JOB_MAX_S() > 0:
            # One uncapped query_combo/wiki_category job made Wikimedia calls for hours (11-25: 2,757 requests in 3 jobs;
            # 11-26: one job ran past the whole fetch budget). Each discover job keeps what it found within its slice.
            job_end = monotonic() + DISCOVER_JOB_MAX_S()
            ctx.api.deadline = job_end if run_api_deadline is None else min(job_end, run_api_deadline)
            ctx.api.deadline_clock = monotonic
        try:
            res = HANDLERS[job["kind"]](ctx, payload, job)
            scheduler.finish(ctx.infra, job["job_id"], res, now)
        except Exception as e:  # bounded retry with backoff; dead-letter after MAX_ATTEMPTS
            state = scheduler.fail(ctx.infra, job, "%s: %s" % (type(e).__name__, e), now)
            res = {"error": type(e).__name__, "state": state}
            ctx.bump("job_errors")
        if hasattr(ctx.api, "deadline"):
            ctx.api.deadline = run_api_deadline
        used = ctx.fetcher.requests + ctx.api.calls - before
        scheduler.budget_add(ctx.infra, day, job["budget_class"], "requests", used)
        scheduler.checkpoint(ctx.infra, ctx.run_id, "last_job", {"job_id": job["job_id"], "kind": job["kind"], "done": done + 1}, now)
        done += 1
        counters["done"] = done
        if log:
            log("[%s] %-11s %-9s %s" % (job["budget_class"][:4], job["kind"], res.get("state") or res.get("outcome") or res.get("skipped") or "", {k: v for k, v in res.items() if k in ("new", "relevance", "reason", "change", "facts", "state", "requests", "kind")}))
