"""Source evaluator: a small trial crawl scores relevance, novelty vs duplicate, extractability, original vs repost and permitted
scope, then registers or deprioritizes the candidate."""
from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .. import timeutil
from ..db import transaction
from ..profiles import pitchboard as prof
from . import parse, store
from .net import FetchResult, Fetcher, normalize_url, origin_of, registered_domain

TRIAL_PAGES = 4
MIN_RELEVANCE = 0.4
STRONG_FIELDS = set(prof.REQUIRED) - {"status.declared"}


@dataclasses.dataclass
class Evaluation:
    state: str
    reason: str
    scores: Dict
    pages: Dict[str, FetchResult]
    docs: Dict[str, parse.Doc]
    requests: int
    feeds: List[str]
    links: List[str]


def _same_site(a: str, b: str) -> bool:
    return registered_domain(urlparse(a).netloc.lower()) == registered_domain(urlparse(b).netloc.lower())


def trial_crawl(svc: sqlite3.Connection, fetcher: Fetcher, cand: sqlite3.Row, target: dict, now: str) -> Evaluation:
    host = cand["host"]
    start = fetcher.requests
    pages: Dict[str, FetchResult] = {}
    docs: Dict[str, parse.Doc] = {}
    scores: Dict = {"permitted": False}

    def done(state, reason):
        scores["requests"] = fetcher.requests - start
        return Evaluation(state, reason, scores, pages, docs, fetcher.requests - start, feeds, [])

    feeds: List[str] = []
    if store.is_blocked(host):
        return done("rejected", "blocked_domain")
    if store.is_seed(host):
        scores.update(novelty=0.0, duplicate="seed_list")
        return done("registered_existing", "already_in_seed_list")
    rd = registered_domain(host)
    if svc.execute("SELECT 1 FROM pb_source_site WHERE registered_domain=?", (rd,)).fetchone():
        scores.update(novelty=0.0, duplicate="registered_domain")
        return done("registered_existing", "domain_already_registered")
    entry = normalize_url(cand["url"])
    if entry.startswith("http://"):                                        # prefer https; fall back to the given http URL only if https is unusable
        up = "https://" + entry[len("http://"):]
        first = fetcher.fetch(up, now_iso=now)
        if first.outcome == "ok" or first.outcome.startswith("denied"):
            entry = up
        else:
            alt = fetcher.fetch(entry, now_iso=now)
            first = alt if alt.outcome == "ok" or first.outcome == "error" else first
            entry = entry if first is alt else up
    else:
        first = fetcher.fetch(entry, now_iso=now)
    pages[entry] = first
    if first.outcome in ("denied_robots", "denied_http", "blocked_network"):
        scores.update(permitted=False, fail=first.outcome)
        return done("rejected", "not_permitted:" + first.outcome + ":" + first.error_class)
    if first.outcome != "ok":
        scores.update(permitted=True, fail=first.outcome, error_class=first.error_class)
        return done("deprioritized", "entry_unusable:" + first.outcome)
    scores["permitted"] = True
    doc = parse.parse_html(first.body, first.final_url) if first.content_type.startswith(("text/html", "application/xhtml")) else None
    if doc is None:
        return done("deprioritized", "entry_not_html")
    docs[entry] = doc
    feeds = doc.feeds[:2]
    # follow the most call-like same-site links
    ranked = sorted({l.href: l for l in doc.links if _same_site(l.href, first.final_url) and l.href.startswith("http")}.values(),
                    key=lambda l: -prof.link_priority(l))
    follow = [l for l in ranked if prof.link_priority(l) >= 0.5 and not prof.ATTACH_RE.search(l.href)][:TRIAL_PAGES - 1]
    for l in follow:
        u = normalize_url(l.href)
        if u in pages:
            continue
        r = fetcher.fetch(u, now_iso=now)
        pages[u] = r
        if r.outcome == "ok" and r.content_type.startswith(("text/html", "application/xhtml")):
            docs[u] = parse.parse_html(r.body, r.final_url)
        if r.outcome in ("denied_robots", "denied_http"):
            scores["partial_denial"] = scores.get("partial_denial", 0) + 1
    texts, core, call_seen, repost = [], set(), False, False
    for u, d in docs.items():
        texts += [d.title] + [s.text for s in d.segments[:400]] + [l.text for l in d.links[:300]]
        ex = prof.extract(d)
        core |= {e.field for e in ex if e.field in prof.REQUIRED}
        labs = prof.label_doc(d, ex, __import__("datetime").date.fromisoformat(now[:10]))
        call_seen |= any(l.label in ("recruitment", "rules") and l.status not in ("stale_prior_year", "results_not_call", "not_writing_call") for l in labs)
        if d.canonical and not _same_site(d.canonical, u):
            repost = True
        if prof.REPOST_RE.search(" ".join(s.text for s in d.segments[:120])):
            repost = True
    lang = next(iter(docs.values())).lang
    relevance = prof.relevance(texts, lang, target)
    aggregator = any(a in rd for a in prof.AGGREGATORS)
    scores.update(relevance=relevance, extractability=round(len(core) / len(prof.REQUIRED), 3), core_fields=sorted(core), call_pages=bool(call_seen),
                  original=0.0 if (repost or aggregator) else 1.0, novelty=1.0, lang=lang, pages=len(pages))
    if aggregator or repost:
        return done("deprioritized", "repost_or_aggregator")
    if relevance < MIN_RELEVANCE:
        return done("deprioritized", "low_relevance")
    if not (core & STRONG_FIELDS) and not call_seen:
        if relevance >= 0.5 and not core:
            return done("registered", "relevant_pending_site_expansion")
        return done("deprioritized", "not_extractable_no_call_or_fields")
    return done("registered", "ok")
