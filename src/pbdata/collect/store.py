"""Layered storage helpers: raw refs (layer 1), extraction (2), labels (3); sources/sites; seeds and blocklists."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

from .. import timeutil
from ..db import transaction
from ..ids import new_id, sha256_hex, stable_id
from ..rules import sanitize_excerpt
from .net import normalize_url, origin_of, registered_domain

CONFIG = Path(__file__).resolve().parents[3] / "config"


def _load_hosts(name: str) -> Set[str]:
    p = CONFIG / name
    out = set()
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#"):
                out.add(line)
    return out


def seed_hosts() -> Set[str]:
    return _load_hosts("seed_hosts.txt")


def blocked_hosts() -> Set[str]:
    return _load_hosts("blocked_hosts.txt")


def is_blocked(host: str) -> bool:
    d = registered_domain(host.lower())
    return d in blocked_hosts() or host.lower() in blocked_hosts()


def is_seed(host: str) -> bool:
    d = registered_domain(host.lower())
    return d in seed_hosts() or host.lower() in seed_hosts() or host.lower().removeprefix("www.") in seed_hosts()


def _ms(now: str) -> int:
    return int(timeutil.parse_iso(now).timestamp() * 1000)


def register_site(svc: sqlite3.Connection, origin: str, label: str, now: str, *, target_id: str, qid: Optional[str] = None,
                  authority: str = "official_primary", permission: str = "allowed_public", basis: str = "robots_allow+trial_crawl",
                  source_type: str = "official_guidelines", max_epistemic: str = "verified_fact") -> str:
    host = urlparse(origin).netloc.lower()
    sid = stable_id("src", origin + "/")
    with transaction(svc):
        if svc.execute("SELECT 1 FROM pb_source WHERE source_id=?", (sid,)).fetchone() is None:
            oid = new_id("out", _ms(now))
            svc.execute("INSERT INTO pb_outlet(outlet_id,canonical_name,created_at) VALUES(?,?,?)", (oid, label[:120] or host, now))
            svc.execute("INSERT INTO pb_source(source_id,canonical_url,host,source_type,authority,owner_outlet_id,owner_name,permission,"
                        "permission_basis,permission_checked_on,max_epistemic,refresh_hours) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, origin + "/", host, source_type, authority, oid, label[:120], permission, basis, now[:10], max_epistemic, 24))
            svc.execute("INSERT INTO pb_source_site(source_id,outlet_id,origin,registered_domain,in_seed_list,wikidata_qid,target_id) VALUES(?,?,?,?,?,?,?)",
                        (sid, oid, origin, registered_domain(host), 1 if is_seed(host) else 0, qid, target_id))
            svc.execute("INSERT OR IGNORE INTO pb_target_hit VALUES(?,?,?,?,?)", (target_id, "source", sid, now, 1 if is_seed(host) else 0))
            svc.execute("INSERT OR IGNORE INTO pb_target_hit VALUES(?,?,?,?,?)", (target_id, "outlet", oid, now, 1 if is_seed(host) else 0))
    return sid


def site_row(svc: sqlite3.Connection, source_id: str) -> Optional[sqlite3.Row]:
    return svc.execute("SELECT s.*, ss.outlet_id AS site_outlet, ss.origin, ss.target_id FROM pb_source s JOIN pb_source_site ss USING(source_id) WHERE s.source_id=?", (source_id,)).fetchone()


def ensure_page_source(svc: sqlite3.Connection, url: str, site_source_id: str, now: str) -> str:
    url = normalize_url(url)
    sid = stable_id("src", url)
    if svc.execute("SELECT 1 FROM pb_source WHERE source_id=?", (sid,)).fetchone():
        return sid
    site = site_row(svc, site_source_id)
    svc.execute("INSERT INTO pb_source(source_id,canonical_url,host,source_type,authority,owner_outlet_id,owner_name,permission,permission_basis,"
                "permission_checked_on,max_epistemic,refresh_hours) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, url, urlparse(url).netloc.lower(), "official_guidelines", site["authority"], site["owner_outlet_id"], site["owner_name"],
                 site["permission"], "inherits_site:" + site_source_id, now[:10], site["max_epistemic"], 24))
    return sid


def last_raw(svc: sqlite3.Connection, url: str, ok_only: bool = True) -> Optional[sqlite3.Row]:
    q = "SELECT * FROM pb_raw_ref WHERE url=?" + (" AND outcome='ok'" if ok_only else "") + " ORDER BY rowid DESC LIMIT 1"
    return svc.execute(q, (normalize_url(url),)).fetchone()


def seg_hash(text: str) -> str:
    return sha256_hex(" ".join(text.split()).lower())[:12]


def insert_raw(svc: sqlite3.Connection, now: str, **kw) -> str:
    raw_id = new_id("raw", _ms(now))
    cols = ["raw_id"] + list(kw)
    svc.execute("INSERT INTO pb_raw_ref(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))), [raw_id] + [kw[c] for c in kw])
    return raw_id


def ensure_revision(svc: sqlite3.Connection, source_id: str, text_hash: str, seg_hashes: List[str], now: str, extractor_version: str) -> str:
    row = svc.execute("SELECT revision_id FROM pb_source_revision WHERE source_id=? AND content_hash=?", (source_id, text_hash)).fetchone()
    if row:
        return row["revision_id"]
    prev = svc.execute("SELECT revision_id, segment_hashes FROM pb_source_revision WHERE source_id=? ORDER BY rowid DESC LIMIT 1", (source_id,)).fetchone()
    changed = []
    if prev:
        old = json.loads(prev["segment_hashes"])
        changed = [i for i, h in enumerate(seg_hashes) if i >= len(old) or old[i] != h]
    rid = new_id("rev", _ms(now))
    svc.execute("INSERT INTO pb_source_revision(revision_id,source_id,content_hash,segment_hashes,prev_revision_id,change_state,changed_segments,fetched_at,extracted_version)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, source_id, text_hash, json.dumps(seg_hashes), prev["revision_id"] if prev else None, "changed" if prev else "new",
                 json.dumps(changed[:200]), now, extractor_version))
    return rid


def put_extractions(svc: sqlite3.Connection, raw_id: str, exts: Iterable, extractor: str, version: str, now: str) -> int:
    n = 0
    for e in exts:
        excerpt = sanitize_excerpt(e.excerpt) or "-"
        cur = svc.execute("INSERT OR IGNORE INTO pb_extraction(extraction_id,raw_id,field,value_state,value_json,excerpt,locator,extractor,extractor_version,confidence,created_at)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (new_id("ext", _ms(now)), raw_id, e.field, e.value_state, json.dumps(e.value, ensure_ascii=False, sort_keys=True) if e.value is not None else None,
                           excerpt, json.dumps(e.locator, ensure_ascii=False, sort_keys=True), extractor, version, e.confidence, now))
        n += cur.rowcount
    return n


def put_labels(svc: sqlite3.Connection, raw_id: str, labels, version: str, now: str) -> None:
    for l in labels:
        svc.execute("INSERT OR IGNORE INTO pb_label(label_id,raw_id,label,basis,status,open_call_eligible,rule_ids,years_seen,labeler_version,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (new_id("lab", _ms(now)), raw_id, l.label, l.basis, l.status, 1 if l.open_call_eligible else 0,
                     json.dumps(l.rules), json.dumps(l.years), version, now))


def alert(infra: sqlite3.Connection, kind: str, url: str, detail: dict, raw_id: Optional[str], now: str) -> None:
    infra.execute("INSERT OR IGNORE INTO alert(alert_id,kind,url,detail,raw_id,created_at) VALUES(?,?,?,?,?,?)",
                  (new_id("alr", _ms(now)), kind, url, json.dumps(detail, sort_keys=True), raw_id or "", now))
