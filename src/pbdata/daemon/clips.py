"""Manual clip intake (Instagram and other social posts). A human copies the visible notice text into
$ROOT/inbox/manual_clips/*.json; nothing is fetched from instagram.com. Clips enter as source_claim at most
(max_epistemic=source_claim), never verified_fact, and only attach to an outlet the clipper named explicitly
(outlet_id, or an exact name/domain alias). No relation is guessed.

{"url": "https://www.instagram.com/p/XXXX/", "clipped_by": "human:...", "clipped_at": "2026-10-28T01:00:00Z",
 "outlet_id": "out_..." | "outlet_name": "...", "text": "visible post text", "posted_at": "optional ISO"}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from typing import Dict

from .. import facts, timeutil
from ..ids import sha256_hex, stable_id
from ..model import FactInput, ValidationError
from ..collect import parse, store
from ..collect.resolve import norm_name
from ..profiles import pitchboard as prof

CLIP_EXTRACTOR = "manual_clip:rules"


def _outlet(svc, clip: dict):
    if clip.get("outlet_id"):
        r = svc.execute("SELECT outlet_id FROM pb_outlet WHERE outlet_id=?", (clip["outlet_id"],)).fetchone()
        return r[0] if r else None
    name = clip.get("outlet_name")
    if not name:
        return None
    r = svc.execute("SELECT outlet_id FROM pb_outlet WHERE canonical_name=?", (name,)).fetchone() or \
        svc.execute("SELECT subject_id FROM pb_alias WHERE subject_type='outlet' AND alias_norm=?", (norm_name(name),)).fetchone()
    return r[0] if r else None


def ingest_dir(svc, root: str, now: str) -> Dict[str, int]:
    inbox = os.path.join(root, "inbox", "manual_clips")
    out = {"clips_seen": 0, "clips_ingested": 0, "clips_unresolved": 0, "clips_invalid": 0, "clip_facts": 0}
    if not os.path.isdir(inbox):
        os.makedirs(inbox, mode=0o700, exist_ok=True)
        return out
    for sub in ("done", "unresolved", "invalid"):
        os.makedirs(os.path.join(inbox, sub), exist_ok=True)
    for fn in sorted(os.listdir(inbox)):
        p = os.path.join(inbox, fn)
        if not fn.endswith(".json") or not os.path.isfile(p):
            continue
        out["clips_seen"] += 1
        try:
            clip = json.load(open(p, encoding="utf-8"))
            assert clip["url"].startswith("http") and clip["text"].strip() and clip["clipped_by"].startswith("human:")
        except Exception:
            out["clips_invalid"] += 1
            shutil.move(p, os.path.join(inbox, "invalid", fn))
            continue
        outlet = _outlet(svc, clip)
        if outlet is None:
            out["clips_unresolved"] += 1
            shutil.move(p, os.path.join(inbox, "unresolved", fn))
            continue
        out["clip_facts"] += ingest_one(svc, clip, outlet, now)
        out["clips_ingested"] += 1
        shutil.move(p, os.path.join(inbox, "done", fn))
    return out


def ingest_one(svc, clip: dict, outlet_id: str, now: str) -> int:
    url = clip["url"].strip()
    sid = stable_id("src", url)
    host = (url.split("/")[2] if "//" in url else url).lower()
    svc.execute("INSERT OR IGNORE INTO pb_source(source_id,canonical_url,host,source_type,authority,owner_outlet_id,permission,permission_basis,"
                "permission_checked_on,max_epistemic,refresh_hours,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, url, host, "manual_clip", "attributed_secondary", outlet_id, "manual_only", "human manual clip; never auto-fetched",
                 now[:10], "source_claim", 24 * 365, "clipped_by=%s" % clip["clipped_by"][:60]))
    text = clip["text"].strip()
    th = sha256_hex(text)
    chk = stable_id("chk", sid, th)
    svc.execute("INSERT OR IGNORE INTO pb_source_check(check_id,source_id,checked_at,content_hash,segment_count,collection_path,processed) VALUES(?,?,?,?,?,?,1)",
                (chk, sid, clip.get("clipped_at") or now, th, 1, "manual_clip"))
    rev = store.ensure_revision(svc, sid, th, [store.seg_hash(text)], now, CLIP_EXTRACTOR)
    rev_id = rev if isinstance(rev, str) else rev[0]
    doc = parse.Doc(kind="html", title="", segments=[parse.Segment(i, "p", line.strip()) for i, line in enumerate(text.splitlines()) if line.strip()])
    n = 0
    for e in prof.extract(doc):
        if e.value_state not in ("stated", "explicitly_none") or not e.excerpt:
            continue
        try:
            ev = facts.add_evidence(svc, rev_id, sid, e.excerpt, "", int(e.locator.get("seg", 0)), 0, now)
            f = FactInput("outlet", outlet_id, e.field, sid, e.value_state, "source_claim", now, CLIP_EXTRACTOR, prof.EXTRACTOR_VERSION,
                          value=e.value, evidence_id=ev, inspected_revision_id=rev_id, source_authority="attributed_secondary",
                          effective_from=clip.get("posted_at"))
            facts.append(svc, f, now)
            n += 1
        except ValidationError:
            continue
    return n
