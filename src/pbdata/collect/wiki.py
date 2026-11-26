"""Structured discovery sources: MediaWiki/Wikidata public APIs (designed for programmatic use; identified UA, >=1s spacing)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from . import egress

API_UA = os.environ.get("PITCHBOARD_API_UA", "PitchboardBot/0.1 (research collector; contact hanset@soulbound.co.kr)")


class Api:
    def __init__(self, min_interval: float = 1.0, cap: Optional[int] = None, sleep=time.sleep):
        self.last = 0.0
        self.min_interval = min_interval
        self.calls = 0
        self.cap = cap
        self.sleep = sleep
        self.errors = 0
        self.deadline: Optional[float] = None       # set by collector.run: no new API calls once the run's deadline passes
        self.deadline_clock = time.time

    def get(self, base: str, params: Dict[str, str], accept: str = "application/json", timeout: float = 40) -> Optional[dict]:
        if self.cap is not None and self.calls >= self.cap:
            return None
        if self.deadline is not None and self.deadline_clock() >= self.deadline:
            return None
        wait = self.min_interval - (time.time() - self.last)
        if wait > 0:
            self.sleep(wait)
        self.last = time.time()
        self.calls += 1
        url = base + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": API_UA, "Accept": accept})
        try:
            with egress.urlopen(req, timeout=timeout) as r:
                data = r.read(8_000_000)
            if egress.configured_proxy():
                egress.log_traffic(len(data), urllib.parse.urlparse(url).hostname or "")
            return json.loads(data.decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            self.errors += 1
            return None

    # ---- MediaWiki
    def _wiki_titles(self, lang, module, params, prefix, cursor_key, limit):
        out, continuation, seen = [], {}, set()
        base = "https://%s.wikipedia.org/w/api.php" % lang
        while limit is None or len(out) < limit:
            size = 50 if limit is None else min(50, limit - len(out))
            d = self.get(base, {"action": "query", "list": module, "format": "json",
                                prefix + "limit": str(size), **params, **continuation})
            if not d:
                break
            out.extend(m["title"] for m in d.get("query", {}).get(module, []))
            next_page = d.get("continue") or {}
            cursor = next_page.get(cursor_key)
            if cursor is None or cursor in seen:
                break
            seen.add(cursor)
            continuation = {k: str(next_page[k]) for k in ("continue", cursor_key) if k in next_page}
        return out[:limit]

    def search(self, lang: str, text: str, limit: Optional[int] = None) -> List[str]:
        return self._wiki_titles(lang, "search", {"srsearch": text, "srnamespace": "0"}, "sr", "sroffset", limit)

    def category_members(self, lang: str, category: str, limit: Optional[int] = None) -> List[str]:
        return self._wiki_titles(lang, "categorymembers", {"cmtitle": category, "cmnamespace": "0"},
                                 "cm", "cmcontinue", limit)

    def qids_for_titles(self, lang: str, titles: List[str]) -> Dict[str, str]:
        out = {}
        for i in range(0, len(titles), 40):
            d = self.get("https://%s.wikipedia.org/w/api.php" % lang, {"action": "query", "prop": "pageprops", "ppprop": "wikibase_item", "titles": "|".join(titles[i:i + 40]), "format": "json", "redirects": "1"})
            for p in (d or {}).get("query", {}).get("pages", {}).values():
                q = p.get("pageprops", {}).get("wikibase_item")
                if q:
                    out[p["title"]] = q
        return out

    # ---- Wikidata
    def entity_sites(self, qids: List[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for i in range(0, len(qids), 40):
            d = self.get("https://www.wikidata.org/w/api.php", {"action": "wbgetentities", "ids": "|".join(qids[i:i + 40]), "props": "labels|claims|descriptions", "languages": "en|ko", "format": "json"})
            for q, e in (d or {}).get("entities", {}).items():
                sites = []
                for c in e.get("claims", {}).get("P856", []):
                    v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
                    if v and c.get("rank") != "deprecated":
                        sites.append(v)
                lab = e.get("labels", {})
                # no en/ko label -> empty, never the bare QID (a QID is not a name; downstream falls back to title/host)
                out[q] = {"label": (lab.get("en") or lab.get("ko") or {}).get("value", ""), "sites": sites,
                          "desc": (e.get("descriptions", {}).get("en") or e.get("descriptions", {}).get("ko") or {}).get("value", "")}
        return out

    def resolve_class(self, text: str, lang: str = "en") -> Optional[str]:
        d = self.get("https://www.wikidata.org/w/api.php", {"action": "wbsearchentities", "search": text, "language": lang, "uselang": lang, "type": "item", "limit": "1", "format": "json"})
        r = (d or {}).get("search", [])
        return r[0]["id"] if r else None

    def search_entities(self, text: str, lang: str = "en", limit: Optional[int] = None) -> List[dict]:
        out, cursor, seen = [], None, set()
        while limit is None or len(out) < limit:
            size = 50 if limit is None else min(50, limit - len(out))
            params = {"action": "wbsearchentities", "search": text, "language": lang, "uselang": lang,
                      "type": "item", "limit": str(size), "format": "json"}
            if cursor is not None:
                params["continue"] = str(cursor)
            d = self.get("https://www.wikidata.org/w/api.php", params) or {}
            out.extend(d.get("search", []))
            cursor = d.get("search-continue")
            if cursor is None or cursor in seen:
                break
            seen.add(cursor)
        return out[:limit]

    def sparql(self, query: str) -> List[dict]:
        d = self.get("https://query.wikidata.org/sparql", {"query": query, "format": "json"}, accept="application/sparql-results+json", timeout=90)
        return (d or {}).get("results", {}).get("bindings", [])
