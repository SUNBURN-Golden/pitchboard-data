"""Gap finder: missing required fields become tracked gaps, classed not_extracted / absent_in_source / inaccessible / conflict.
Never AI-fills; a gap only closes with a stated/explicit fact + evidence, or an honest 'absent' after inspection."""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, List, Optional

from .. import facts, timeutil
from ..db import transaction
from ..ids import new_id
from ..model import FactInput, ValidationError
from ..profiles import pitchboard as prof

DENIAL = ("login_wall", "challenge", "denied_robots", "denied_http", "needs_render", "blocked_network")


def _live(svc, subject_id, predicate):
    return svc.execute("SELECT * FROM pb_fact WHERE subject_id=? AND predicate=? AND superseded_at IS NULL", (subject_id, predicate)).fetchall()


def site_pages(svc, outlet_id: str):
    return svc.execute(
        "SELECT r.* FROM pb_raw_ref r JOIN pb_source s ON s.source_id=r.source_id WHERE s.owner_outlet_id=? AND r.kind IN ('html','pdf','image') "
        "AND r.rowid IN (SELECT max(rowid) FROM pb_raw_ref GROUP BY url)", (outlet_id,)).fetchall()


def compute_gaps(svc: sqlite3.Connection, outlet_id: str, target_id: str, now: str) -> Dict[str, int]:
    counts = {"open": 0, "filled": 0, "closed_absent": 0, "blocked": 0}
    pages = site_pages(svc, outlet_id)
    ok_pages = [p for p in pages if p["outcome"] == "ok"]
    denied = [p for p in pages if p["outcome"] in DENIAL]
    relevant_ok = []
    for p in ok_pages:
        labs = {l["label"]: l for l in svc.execute("SELECT * FROM pb_label WHERE raw_id=?", (p["raw_id"],))}
        if ("rules" in labs) or ("recruitment" in labs and labs["recruitment"]["status"] not in ("stale_prior_year", "results_not_call", "past_deadline", "not_writing_call")):
            relevant_ok.append(p)
    calls = svc.execute("SELECT call_id FROM pb_call_edition WHERE outlet_id=? AND status IN ('announced','open','extended','standing')", (outlet_id,)).fetchall()
    for pred, (stype, importance) in prof.REQUIRED.items():
        if stype == "call":
            subjects = [("call", c["call_id"]) for c in calls] or [("outlet", outlet_id)]
        else:
            subjects = [("outlet", outlet_id)]
        for st, sid in subjects:
            live = _live(svc, sid, pred)
            good = [f for f in live if f["value_state"] in ("stated", "explicitly_none")]
            gap = svc.execute("SELECT * FROM pb_gap WHERE subject_id=? AND predicate=? AND scope_ref='unspecified'", (sid, pred)).fetchone()
            if good:
                if gap and gap["state"] != "filled":
                    with transaction(svc):
                        svc.execute("UPDATE pb_gap SET state='filled', filled_fact_id=?, filled_evidence_id=?, resolved_at=? WHERE gap_id=?",
                                    (good[0]["fact_id"], good[0]["evidence_id"], now, gap["gap_id"]))
                counts["filled"] += 1 if gap and gap["state"] != "filled" else 0
                continue
            if any(f["conflict_group_id"] and svc.execute(
                    "SELECT 1 FROM pb_conflict_group WHERE group_id=? AND resolved_at IS NULL",
                    (f["conflict_group_id"],)).fetchone() for f in live):
                cls = "conflict"
            else:
                raw_ids = [p["raw_id"] for p in relevant_ok]
                cue = False
                if raw_ids:
                    q = ",".join("?" * len(raw_ids))
                    cue = svc.execute("SELECT 1 FROM pb_extraction WHERE field='signal.cue' AND json_extract(value_json,'$.for')=? AND raw_id IN (%s) LIMIT 1" % q,
                                      [pred] + raw_ids).fetchone() is not None
                if cue:
                    cls = "not_extracted"
                elif relevant_ok:
                    cls = "absent_in_source" if len(relevant_ok) >= 1 else "not_extracted"
                elif denied and not ok_pages:
                    cls = "inaccessible"
                else:
                    cls = "not_extracted"
            if cls == "absent_in_source" and relevant_ok:
                _record_absence(svc, st, sid, pred, relevant_ok[0], now)
            with transaction(svc):
                if gap is None:
                    svc.execute("INSERT INTO pb_gap(gap_id,subject_type,subject_id,target_id,predicate,gap_class,state,importance,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                (new_id("gap", int(timeutil.parse_iso(now).timestamp() * 1000)), st, sid, target_id, pred, cls, "open", importance, now))
                elif gap["state"] in ("open", "blocked", "closed_absent"):
                    svc.execute("UPDATE pb_gap SET gap_class=? WHERE gap_id=?", (cls, gap["gap_id"]))
            if cls == "inaccessible":
                with transaction(svc):
                    svc.execute("UPDATE pb_gap SET state='blocked', note='access denied/login/needs render: not retried, never bypassed' WHERE subject_id=? AND predicate=? AND state='open'", (sid, pred))
            counts["open"] += 1
    return counts


def _record_absence(svc, st, sid, pred, raw, now) -> None:
    """Persist 'not stated in inspected scope' as an explicit, evidence-less-by-design fact tied to the inspected revision."""
    rev = svc.execute("SELECT revision_id FROM pb_source_revision WHERE source_id=? AND content_hash=?", (raw["source_id"], raw["text_hash"])).fetchone()
    if rev is None:
        return
    if svc.execute("SELECT 1 FROM pb_fact WHERE subject_id=? AND predicate=? AND superseded_at IS NULL", (sid, pred)).fetchone():
        return          # already known, or already recorded as absent for this source: rerun stays idempotent
    try:
        facts.append(svc, FactInput(st, sid, pred, raw["source_id"], "not_stated_in_inspected_scope", "source_claim", now, "rules", prof.EXTRACTOR_VERSION,
                                    inspected_revision_id=rev["revision_id"], source_authority="official_primary"), now)
    except ValidationError:
        pass


def close_absent(svc: sqlite3.Connection, max_attempts: int, now: str) -> int:
    """After bounded attempts a gap classed absent_in_source is closed (honestly: the source does not state it)."""
    with transaction(svc):
        c = svc.execute("UPDATE pb_gap SET state='closed_absent', resolved_at=? WHERE state='open' AND gap_class='absent_in_source' AND attempts>=?", (now, max_attempts))
        b = svc.execute("UPDATE pb_gap SET state='blocked', note='attempts exhausted: needs rule or human review' WHERE state='open' AND gap_class IN ('not_extracted','conflict') AND attempts>=?", (max_attempts,))
    return c.rowcount + b.rowcount
