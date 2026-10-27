"""Conservative entity resolution for sources/outlets. Merging is only done on strong evidence (same registered domain, or identical
normalised name); anything weaker stays separate. A false merge is worse than a duplicate, so near-matches are never merged."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Optional

from .net import registered_domain

_STOP = {"the", "a", "an", "of", "magazine", "review", "journal", "literary", "press", "books", "publishing", "publishers", "공식", "홈페이지"}


def norm_name(name: str) -> str:
    n = unicodedata.normalize("NFKC", name or "").lower()
    n = re.sub(r"[^\w\s]", " ", n)
    return " ".join(n.split())


def core_name(name: str) -> str:
    return " ".join(w for w in norm_name(name).split() if w not in _STOP)


def same_entity(name_a: str, host_a: str, name_b: str, host_b: str) -> bool:
    if host_a and host_b and registered_domain(host_a) == registered_domain(host_b):
        return True
    a, b = norm_name(name_a), norm_name(name_b)
    if not a or not b:
        return False
    if a == b:
        return True
    # "The Paris Review" == "Paris Review": only the article differs
    strip = lambda s: re.sub(r"^(the|a|an)\s+", "", s)
    return strip(a) == strip(b)


def find_existing(svc: sqlite3.Connection, name: str, host: str) -> Optional[str]:
    for r in svc.execute("SELECT o.outlet_id, o.canonical_name, ss.origin FROM pb_outlet o JOIN pb_source_site ss ON ss.outlet_id=o.outlet_id"):
        h = r["origin"].split("//", 1)[-1]
        if same_entity(name, host, r["canonical_name"], h):
            return r["outlet_id"]
    return None
