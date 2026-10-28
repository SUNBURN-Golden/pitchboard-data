"""Service projection (pb_search_projection): what search reads. Built only from live (non-superseded) facts.
A condition with no live fact is rendered as 'unknown' -- never as 'no restriction'."""
from __future__ import annotations

import datetime as dt
import json
from typing import Dict, List, Optional

from ..ids import sha256_hex
from .. import timeutil

# ontology chain: outlet/organizer -> call edition -> genre & eligibility -> pay/fee/rights -> deadline
CONDITIONS = ["scope.genre", "scope.submission_category", "eligibility.region", "eligibility.statement",
              "compensation.rate", "fee.submission", "rights.simultaneous", "rights.statement", "policy.ai",
              "deadline.opens_on", "deadline.closes_on", "status.declared", "channel.url"]
LIVE_STATUSES = ("open", "extended", "standing")
DROP_STATUSES = ("superseded", "archived")


def _fact_view(svc, f) -> dict:
    ev = svc.execute("SELECT e.excerpt, e.heading, e.paragraph_index, e.char_start, e.char_end, s.canonical_url FROM pb_evidence e "
                     "JOIN pb_source s ON s.source_id=e.source_id WHERE e.evidence_id=?", (f["evidence_id"],)).fetchone() if f["evidence_id"] else None
    src = svc.execute("SELECT canonical_url, source_type FROM pb_source WHERE source_id=?", (f["source_id"],)).fetchone()
    now = timeutil.iso(timeutil.utcnow())
    stale = bool(f["fresh_until"] and f["fresh_until"] < now)
    return {"fact_id": f["fact_id"], "value": json.loads(f["value_json"]) if f["value_json"] else None, "value_state": f["value_state"],
            "epistemic": f["epistemic"], "stale": stale, "collected_at": f["collected_at"], "effective_from": f["effective_from"],
            "effective_to": f["effective_to"], "fresh_until": f["fresh_until"], "conflict_group_id": f["conflict_group_id"],
            "source": {"url": src["canonical_url"], "type": src["source_type"]},
            "evidence": None if ev is None else {"url": ev["canonical_url"], "excerpt": ev["excerpt"], "heading": ev["heading"],
                                                 "paragraph": ev["paragraph_index"], "chars": [ev["char_start"], ev["char_end"]]}}


def _condition(svc, subject_ids: List[str], predicate: str) -> dict:
    """Return the condition cell. Call-level facts win over outlet-level ones. Conflicts are surfaced, not resolved."""
    for sid in subject_ids:
        rows = svc.execute("SELECT * FROM pb_fact WHERE subject_id=? AND predicate=? AND superseded_at IS NULL ORDER BY recorded_at DESC",
                           (sid, predicate)).fetchall()
        if not rows:
            continue
        views = [_fact_view(svc, r) for r in rows]
        conflict = any(v["conflict_group_id"] for v in views) and len({json.dumps(v["value"], sort_keys=True) for v in views}) > 1
        best = views[0]
        st = best["value_state"]
        answer = ("conflict" if conflict else
                  "stated" if st == "stated" else "explicitly_none" if st == "explicitly_none" else "unknown")
        return {"answer": answer, "from": "call" if sid.startswith("call_") else "outlet", "facts": views}
    return {"answer": "unknown", "reason": "no fact collected (missing condition is NOT 'no restriction')", "facts": []}


def deadline_bounds(v: dict):
    tz = v.get("tz_source", "unknown")
    off = v.get("utc_offset_hours")
    if tz == "explicit" and off is None:
        tz = "unknown"
    return timeutil.deadline_interval(v["date"], tz, off, v.get("time"))


def applicable_now(status: str, closes_on, now: dt.datetime, fresh_until: Optional[str]) -> str:
    """DESIGN 3.2: yes / maybe (deadline window straddles now; tz unknown) / no / unknown."""
    if status in ("closed", "cancelled", "superseded", "archived"):
        return "no"
    if status in ("status_unknown", "candidate", "announced"):
        return "unknown" if status != "announced" else "no"
    if fresh_until and fresh_until < timeutil.iso(now):
        return "unknown"
    if status == "standing" and not closes_on:
        return "yes"
    if not closes_on:
        return "unknown"
    earliest, latest = deadline_bounds(closes_on if isinstance(closes_on, dict) else {"date": closes_on})
    if now < earliest:
        return "yes"
    if now > latest:
        return "no"
    return "maybe"


def build_payload(svc, call_id: str, now: Optional[dt.datetime] = None) -> dict:
    now = now or timeutil.utcnow()
    c = svc.execute("SELECT * FROM pb_call_edition WHERE call_id=?", (call_id,)).fetchone()
    owner_id = c["outlet_id"] or c["organizer_id"]
    tbl = "pb_outlet" if c["outlet_id"] else "pb_organizer"
    col = "outlet_id" if c["outlet_id"] else "organizer_id"
    owner = svc.execute("SELECT canonical_name FROM %s WHERE %s=?" % (tbl, col), (owner_id,)).fetchone()
    aliases = [r[0] for r in svc.execute("SELECT alias_raw FROM pb_alias WHERE subject_id IN (?,?) ORDER BY alias_raw", (owner_id, call_id))]
    conds = {p: _condition(svc, [call_id, owner_id], p) for p in CONDITIONS}
    closes = None
    cl = conds["deadline.closes_on"]
    if cl["answer"] == "stated" and (cl["facts"][0]["value"] or {}).get("date"):
        closes = cl["facts"][0]["value"]
    elif cl["answer"] == "conflict":                     # conflicting deadlines: use the latest so we never hide a live call; flagged for review
        closes = max((f["value"] for f in cl["facts"] if f["value"] and f["value"].get("date")), key=lambda v: v["date"], default=None)
    elif c["edition_key"][:4].isdigit() and len(c["edition_key"]) == 10:
        closes = {"date": c["edition_key"]}
    fresh = min([f["fresh_until"] for p in ("deadline.closes_on", "status.declared") for f in conds[p]["facts"] if f["fresh_until"]] or [None]) \
        if any(conds[p]["facts"] for p in ("deadline.closes_on", "status.declared")) else None
    history = [dict(r) for r in svc.execute("SELECT kind, from_status, to_status, basis, evidence_id, occurred_at, reason FROM pb_status_event "
                                           "WHERE call_id=? ORDER BY recorded_at", (call_id,))]
    revs = sorted({r[0] for r in svc.execute("SELECT DISTINCT e.revision_id FROM pb_fact f JOIN pb_evidence e ON e.evidence_id=f.evidence_id "
                                              "WHERE f.subject_id IN (?,?) AND f.superseded_at IS NULL", (call_id, owner_id))})
    return {"call_id": call_id, "owner": {"id": owner_id, "type": col[:-3], "name": owner["canonical_name"] if owner else None, "aliases": aliases},
            "edition_key": c["edition_key"], "title": c["title_raw"], "scope_ref": c["scope_ref"],
            "status": c["status"], "status_since": c["status_since"], "status_basis": c["status_basis"],
            "applicable_now": applicable_now(c["status"], closes, now, fresh), "closes_on": closes["date"] if closes else None,
            "conditions": conds, "status_history": history, "source_revision_ids": revs}


def recompute(svc, now_iso: str, only_dirty: bool = True) -> Dict[str, int]:
    out = {"recomputed": 0, "unchanged": 0, "tombstoned": 0}
    rows = svc.execute("SELECT c.call_id, c.status, c.dirty_seq, p.projection_version, p.payload_hash, p.built_for_dirty_seq FROM pb_call_edition c "
                       "LEFT JOIN pb_search_projection p ON p.call_id=c.call_id").fetchall()
    for r in rows:
        if r["status"] in DROP_STATUSES:
            if r["projection_version"] is not None:
                svc.execute("INSERT INTO pb_projection_tombstone(call_id,removed_at,reason,last_version) VALUES(?,?,?,?)",
                            (r["call_id"], now_iso, "status:" + r["status"], r["projection_version"]))
                svc.execute("DELETE FROM pb_search_projection WHERE call_id=?", (r["call_id"],))
                out["tombstoned"] += 1
            continue
        # applicable_now depends on the clock, so every live call is re-evaluated; version bumps only when the payload changes
        payload = build_payload(svc, r["call_id"], timeutil.parse_iso(now_iso))
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        h = sha256_hex(body)
        if r["payload_hash"] == h and r["built_for_dirty_seq"] == r["dirty_seq"]:
            out["unchanged"] += 1
            continue
        ver = (r["projection_version"] or 0) + 1
        svc.execute("INSERT INTO pb_search_projection(call_id,projection_version,payload,payload_hash,as_of,source_revision_ids,computed_at,built_for_dirty_seq)"
                    " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET projection_version=excluded.projection_version, payload=excluded.payload,"
                    " payload_hash=excluded.payload_hash, as_of=excluded.as_of, source_revision_ids=excluded.source_revision_ids,"
                    " computed_at=excluded.computed_at, built_for_dirty_seq=excluded.built_for_dirty_seq",
                    (r["call_id"], ver, body, h, now_iso, json.dumps(payload["source_revision_ids"]), now_iso, r["dirty_seq"]))
        svc.execute("INSERT INTO pb_outbox(event_id,kind,subject_id,payload,created_at) VALUES(?,?,?,?,?)",
                    ("evt_proj_%s_%d" % (r["call_id"], ver), "projection_updated", r["call_id"], json.dumps({"version": ver}), now_iso))
        out["recomputed"] += 1
    return out
