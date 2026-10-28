"""Structured discovery sources: MediaWiki/Wikidata public APIs (designed for programmatic use; identified UA, >=1s spacing)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from .net import USER_AGENT

API_UA = os.environ.get("PITCHBOARD_API_UA", "PitchboardBot/0.1 (research collector; contact hanset@soulbound.co.kr)")


class Api:
    def __init__(self, min_interval: float = 1.0, cap: Optional[int] = None, sleep=time.sleep):
        self.last = 0.0
        self.min_interval = min_interval
        self.calls = 0
        self.cap = cap
        self.sleep = sleep
        self.errors = 0

    def get(self, base: str, params: Dict[str, str], accept: str = "application/json", timeout: float = 40) -> Optional[dict]:
        if self.cap is not None and self.calls >= self.cap:
            return None
        wait = self.min_interval - (time.time() - self.last)
        if wait > 0:
            self.sleep(wait)
        self.last = time.time()
        self.calls += 1
        url = base + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": API_UA, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read(8_000_000).decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            self.errors += 1
            return None

    # ---- MediaWiki
    def search(self, lang: str, text: str, limit: int = 8) -> List[str]:
        d = self.get("https://%s.wikipedia.org/w/api.php" % lang, {"action": "query", "list": "search", "srsearch": text, "srlimit": str(limit), "format": "json", "srnamespace": "0"})
        return [r["title"] for r in (d or {}).get("query", {}).get("search", [])]

    def category_members(self, lang: str, category: str, limit: int = 60) -> List[str]:
        out, cont = [], None
        while len(out) < limit:
            p = {"action": "query", "list": "categorymembers", "cmtitle": category, "cmlimit": str(min(50, limit - len(out))), "format": "json", "cmnamespace": "0"}
            if cont:
                p["cmcontinue"] = cont
            d = self.get("https://%s.wikipedia.org/w/api.php" % lang, p)
            if not d:
                break
            out += [m["title"] for m in d.get("query", {}).get("categorymembers", [])]
            cont = d.get("continue", {}).get("cmcontinue")
            if not cont:
                break
        return out

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
                out[q] = {"label": (lab.get("en") or lab.get("ko") or {}).get("value", q), "sites": sites,
                          "desc": (e.get("descriptions", {}).get("en") or e.get("descriptions", {}).get("ko") or {}).get("value", "")}
        return out

    def resolve_class(self, text: str, lang: str = "en") -> Optional[str]:
        d = self.get("https://www.wikidata.org/w/api.php", {"action": "wbsearchentities", "search": text, "language": lang, "uselang": lang, "type": "item", "limit": "1", "format": "json"})
        r = (d or {}).get("search", [])
        return r[0]["id"] if r else None

    def search_entities(self, text: str, lang: str = "en", limit: int = 3) -> List[dict]:
        d = self.get("https://www.wikidata.org/w/api.php", {"action": "wbsearchentities", "search": text, "language": lang, "uselang": lang, "type": "item", "limit": str(limit), "format": "json"})
        return (d or {}).get("search", [])

    def sparql(self, query: str) -> List[dict]:
        d = self.get("https://query.wikidata.org/sparql", {"query": query, "format": "json"}, accept="application/sparql-results+json", timeout=90)
        return (d or {}).get("results", {}).get("bindings", [])
