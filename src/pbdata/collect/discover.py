"""Source discoverer, driven by a target list (country x language x genre x organizer type x year x event keywords).
Strategies: wikidata_class (SPARQL by class+country/language), wiki_category (directory/org lists), query_combo (entity search over
combinations), entity_followup (names seen in fetched text), site chain (homepage -> sns/application/attachments), gap_search.
Budgets: max API calls, max candidates, max depth, minimum prior relevance."""
from __future__ import annotations

import dataclasses
import itertools
import json
import re
import sqlite3
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse

from .. import timeutil
from ..db import transaction
from ..ids import new_id
from ..profiles import pitchboard as prof
from . import store
from .net import normalize_url, origin_of, registered_domain
from .wiki import Api

EXCLUDE_HOSTS = ("wikipedia.org", "wikimedia.org", "wikidata.org", "archive.org", "web.archive.org", "doi.org", "books.google", "amazon.", "worldcat.org",
                 "jstor.org", "ebscohost", "proquest", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "goo.gl", "bit.ly",
                 "lyellcollection", "sciencedirect", "springer", "wiley.com", "tandfonline", "cambridge.org", "oup.com", "nih.gov", "ncbi.", "oclc.org", "isbndb",
                 "nla.gov.au", "hathitrust", "chadwyck", "revealdigital", "informit", "elsevier.com", "catalog.", "library.", "libraries.", "loc.gov", "bl.uk", "orgs.", "dbpia", "kyobobook", "yes24", "aladin", "naver.com", "daum.net", "google.", "bing.com", "linkedin.com")
WIKI_ORIGIN_CLASS = {"literary magazine": "Q1368848", "book publisher": "Q1320047", "literary award": "Q378427"}  # verified via wbsearchentities 2026-10-27


@dataclasses.dataclass
class Budget:
    api_calls: int = 60
    max_candidates: int = 80
    max_depth: int = 2
    min_prior: float = 0.0


@dataclasses.dataclass
class Cand:
    url: str
    label: str
    strategy: str
    origin_ref: str
    prior: float
    qid: Optional[str] = None


def load_targets(path) -> dict:
    with open(path) as fh:
        return json.load(fh)


def query_combos(target: dict, year: int, limit: int = 40) -> List[str]:
    """Combinations of genre x organizer type x event keyword (+ year, country/language terms) for entity search."""
    out = []
    kws = target.get("event_keywords", []) or [""]
    for g, o, k in itertools.product(target.get("genres", [""]) or [""], target.get("organizer_types", [""]) or [""], kws):
        out.append(" ".join(x for x in (g, o, k) if x))
    for k in kws:
        for o in target.get("organizer_types", []):
            out.append("%s %s %d" % (k, o, year))
    seen, res = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            res.append(q)
    return res[:limit]


def _acceptable(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    h = p.hostname.lower()
    if any(x in h for x in EXCLUDE_HOSTS):
        return False
    return not store.is_blocked(h)


def _from_entities(ents: Dict[str, dict], strategy: str, origin_ref: str, prior: float) -> List[Cand]:
    out = []
    for qid, e in ents.items():
        for s in e["sites"][:1]:
            if _acceptable(s):
                out.append(Cand(s, e["label"], strategy, origin_ref + ":" + qid, prior, qid))
    return out


def wikidata_class(api: Api, target: dict, limit: int = 200) -> List[Cand]:
    cc, lang = target.get("country_qid"), target.get("language_qid")
    out: List[Cand] = []
    for text in target.get("organizer_types", []):
        qid = WIKI_ORIGIN_CLASS.get(text) or api.resolve_class(text)
        if not qid:
            continue
        geo = []
        if cc:
            geo += ["{ ?item wdt:P495 wd:%s }" % cc, "{ ?item wdt:P17 wd:%s }" % cc, "{ ?item wdt:P159/wdt:P17 wd:%s }" % cc, "{ ?item wdt:P291/wdt:P17 wd:%s }" % cc]
        if lang:
            geo.append("{ ?item wdt:P407 wd:%s }" % lang)
        if not geo:
            continue
        q = ("SELECT ?item ?itemLabel ?site WHERE { ?item wdt:P31/wdt:P279* wd:%s . ?item wdt:P856 ?site . %s "
             'SERVICE wikibase:label { bd:serviceParam wikibase:language "en,ko". } } LIMIT %d') % (qid, " UNION ".join(geo), limit)
        for b in api.sparql(q):
            site = b["site"]["value"]
            if _acceptable(site):
                out.append(Cand(site, b.get("itemLabel", {}).get("value", ""), "wikidata_class", "%s/%s:%s" % (target["id"], qid, b["item"]["value"].rsplit("/", 1)[-1]), 0.5,
                                b["item"]["value"].rsplit("/", 1)[-1]))
    return out


def wiki_category(api: Api, target: dict, limit: int = 60) -> List[Cand]:
    out: List[Cand] = []
    for ref in target.get("directory_categories", []):
        lang, cat = ref["lang"], ref["category"]
        titles = api.category_members(lang, cat, limit)
        qmap = api.qids_for_titles(lang, titles)
        ents = api.entity_sites(list(qmap.values()))
        out += _from_entities(ents, "directory_list", "%s:%s" % (lang, cat), 0.45)
    return out


def query_search(api: Api, target: dict, year: int, max_queries: int = 12) -> List[Cand]:
    out: List[Cand] = []
    lang = target.get("language", "en")
    qs = query_combos(target, year)[:max_queries]
    qids: Dict[str, str] = {}
    for q in qs:
        titles = api.search(lang if lang in ("ko", "en", "ja", "de", "fr") else "en", q, 5)
        qids.update({qid: t for t, qid in api.qids_for_titles(lang if lang in ("ko", "en", "ja", "de", "fr") else "en", titles).items()})
    ents = api.entity_sites(list(qids))
    out += _from_entities(ents, "query_combo", target["id"], 0.4)
    return out


def entity_followup(api: Api, name: str, lang: str, parent: str) -> List[Cand]:
    found = api.search_entities(name, lang if lang in ("ko", "en") else "en", 2)
    ents = api.entity_sites([f["id"] for f in found if f.get("id")])
    return _from_entities(ents, "entity_followup", parent, 0.4)


def add_candidates(svc: sqlite3.Connection, cands: Iterable[Cand], target_id: str, now: str, budget: Budget, depth: int = 0,
                   parent: Optional[str] = None) -> List[str]:
    """Insert new candidates (dedupe by host+target). Skips blocked hosts. Returns new cand_ids."""
    new_ids = []
    ms = int(timeutil.parse_iso(now).timestamp() * 1000)
    per = {}
    for c in cands:
        if c.strategy not in per:
            per[c.strategy] = svc.execute("SELECT count(*) FROM pb_discovery_candidate WHERE target_id=? AND strategy=?", (target_id, c.strategy)).fetchone()[0]
        if per[c.strategy] >= budget.max_candidates or c.prior < budget.min_prior or depth > budget.max_depth:
            break
        host = urlparse(c.url).netloc.lower()
        if not host or store.is_blocked(host):
            continue
        cid = new_id("cand", ms)
        with transaction(svc):
            cur = svc.execute("INSERT OR IGNORE INTO pb_discovery_candidate(cand_id,url,host,label,target_id,strategy,origin_ref,parent_cand_id,depth,prior_relevance,state,discovered_at)"
                              " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (cid, c.url, host, c.label[:120], target_id, c.strategy, c.origin_ref, parent, depth, c.prior, "new", now))
        if cur.rowcount:
            new_ids.append(cid)
            per[c.strategy] += 1
    return new_ids


def call_index(fetcher, target: dict, now: str, year: int, max_pages: int = 3) -> List[Cand]:
    """Current-cycle discovery from official call indexes / organiser news boards and open-call directories (config `call_indexes`).
    The index page itself becomes a candidate (strategy call_index); outbound organiser links on directory pages become
    candidates (strategy directory_hub). Directory pages are discovery only: no facts are taken from them."""
    from . import parse
    from ..profiles import pitchboard as prof
    out: List[Cand] = []
    for ix in target.get("call_indexes", []):
        url, kind = ix["url"], ix.get("kind", "official")
        if kind == "official" and _acceptable(url):
            out.append(Cand(url, ix.get("label", ""), "call_index", "%s:%s" % (target["id"], url), 0.8))
        if kind != "directory":
            continue
        pages, seen = [url], set()
        while pages and len(seen) < max_pages:
            u = pages.pop(0)
            if u in seen:
                continue
            seen.add(u)
            r = fetcher.fetch(u, now_iso=now)
            if r.outcome != "ok" or not r.content_type.startswith("text/html"):
                continue
            d = parse.parse_html(r.body, r.final_url)
            pages += [n for n in d.next_pages[:1]]
            own = urlparse(r.final_url).hostname or ""
            links = list(d.links)
            # directories that keep each call on an internal detail page (e.g. Korean contest portals): follow up to
            # `max_details` internal detail links matching `detail_re` and take the organiser links found there (discovery only)
            if ix.get("detail_re"):
                det = [l.href for l in d.links if re.search(ix["detail_re"], l.href) and (urlparse(l.href).hostname or "").removeprefix("www.") == own.removeprefix("www.")]
                for du in list(dict.fromkeys(det))[: int(ix.get("max_details", 12))]:
                    rr = fetcher.fetch(du, now_iso=now)
                    if rr.outcome == "ok" and rr.content_type.startswith("text/html"):
                        links += parse.parse_html(rr.body, rr.final_url).links
            for l in links:
                h = (urlparse(l.href).hostname or "").lower()
                if not h or h.endswith(own.replace("www.", "")) or not _acceptable(l.href):
                    continue
                if prof.cycle_score((l.text or "") + " " + l.href, None) < 0:
                    continue
                out.append(Cand(l.href, (l.text or "")[:120], "directory_hub", "%s:%s" % (target["id"], u), 0.6))
    return out
