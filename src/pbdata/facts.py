"""Fact store: append-only facts, supersession of same-source edits, conflict groups, confirmations, corrections."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
from typing import List, Optional, Union

from . import timeutil
from .db import transaction
from .ids import new_id, sha256_hex, stable_id
from .model import (EPISTEMIC_RANK, FactInput, ValidationError, Violation, dumps, freshness_days)
from .rules import check_excerpt, sanitize_excerpt, validate_fact

UNSTATED = ("not_stated_in_inspected_scope", "not_inspected")


def _ms(now: str) -> int:
    return int(timeutil.parse_iso(now).timestamp() * 1000)


@dataclasses.dataclass
class AppendResult:
    fact_id: str
    action: str            # created | unchanged | superseded_prior | conflict | corroborated | promoted
    superseded: List[str]
    conflict_group_id: Optional[str] = None


def add_evidence(conn: sqlite3.Connection, revision_id: str, source_id: str, excerpt: str, heading: str,
                 paragraph_index: int, char_start: int, observed_at: str, retain_until: Optional[str] = None) -> str:
    """Store a sanitized excerpt (<=280 chars) with hash + locator. Idempotent per (revision, quote, paragraph)."""
    clean = sanitize_excerpt(excerpt)
    problems = check_excerpt(clean)
    if problems:
        raise ValidationError(problems)
    qh = sha256_hex(clean)
    row = conn.execute("SELECT evidence_id FROM pb_evidence WHERE revision_id=? AND quote_hash=? AND paragraph_index=?",
                       (revision_id, qh, paragraph_index)).fetchone()
    if row:
        return row["evidence_id"]
    eid = new_id("ev", _ms(observed_at))
    conn.execute("INSERT INTO pb_evidence(evidence_id,revision_id,source_id,excerpt,quote_hash,heading,paragraph_index,"
                 "char_start,char_end,observed_at,retain_until) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (eid, revision_id, source_id, clean, qh, heading[:120], paragraph_index, char_start,
                  char_start + len(clean), observed_at, retain_until))
    return eid


def _same_value(a: sqlite3.Row, f: FactInput) -> bool:
    return a["value_state"] == f.value_state and (a["value_json"] or None) == dumps(f.value)


def _non_committal(row_or_input: Union[sqlite3.Row, FactInput]) -> bool:
    """An AI mention alone makes no assertion about the outlet's policy."""
    if isinstance(row_or_input, FactInput):
        predicate, value = row_or_input.predicate, row_or_input.value
    else:
        predicate = row_or_input["predicate"]
        value = json.loads(row_or_input["value_json"]) if row_or_input["value_json"] else None
    return predicate == "policy.ai" and isinstance(value, dict) and value.get("state") == "mentioned_unclassified"


def _contradicts(a: sqlite3.Row, b: Union[sqlite3.Row, FactInput]) -> bool:
    """Compare cross-source assertions using the same rule for append and repair."""
    if isinstance(b, FactInput):
        source, state, value_json, predicate = b.source_id, b.value_state, dumps(b.value), b.predicate
    else:
        source, state, value_json, predicate = b["source_id"], b["value_state"], b["value_json"], b["predicate"]
    if a["source_id"] == source:
        return False
    if a["value_state"] == state and (a["value_json"] or None) == (value_json or None):
        return False
    if a["value_state"] in UNSTATED or state in UNSTATED:
        return False  # absence on one page never contradicts another source
    if a["predicate"] == predicate == "policy.ai" and (_non_committal(a) or _non_committal(b)):
        av = json.loads(a["value_json"]) if a["value_json"] else None
        bv = json.loads(value_json) if value_json else None
        if isinstance(av, dict) and isinstance(bv, dict) and av.get("dimension") is not None and av["dimension"] == bv.get("dimension"):
            return False
    return True


def _insert(conn: sqlite3.Connection, f: FactInput, now: str, conflict_group_id: Optional[str]) -> str:
    fid = new_id("fact", _ms(now))
    conn.execute(
        "INSERT INTO pb_fact(fact_id,subject_type,subject_id,predicate,scope_ref,value_json,value_raw,value_state,epistemic,"
        "evidence_id,inspected_revision_id,source_id,source_authority,collected_at,effective_from,effective_to,recorded_at,"
        "verified_at,verifier,fresh_until,conflict_group_id,correction_of,correction_reason,extractor,extractor_version,run_id,"
        "retain_until) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, f.subject_type, f.subject_id, f.predicate, f.scope_ref, dumps(f.value), f.value_raw, f.value_state, f.epistemic,
         f.evidence_id, f.inspected_revision_id, f.source_id, f.source_authority, f.collected_at, f.effective_from,
         f.effective_to, now, f.verified_at, f.verifier, f.fresh_until, conflict_group_id, f.correction_of,
         f.correction_reason, f.extractor, f.extractor_version, f.run_id, f.retain_until))
    return fid


def _supersede(conn: sqlite3.Connection, old_id: str, new_id_: str, now: str) -> None:
    conn.execute("UPDATE pb_fact SET superseded_at=?, superseded_by=? WHERE fact_id=? AND superseded_at IS NULL",
                 (now, new_id_, old_id))


def _outbox(conn: sqlite3.Connection, kind: str, subject_id: str, payload: dict, now: str) -> None:
    conn.execute("INSERT INTO pb_outbox(event_id,kind,subject_id,payload,created_at) VALUES(?,?,?,?,?)",
                 (new_id("evt", _ms(now)), kind, subject_id, dumps(payload), now))


def _mark_call_dirty(conn: sqlite3.Connection, f: FactInput) -> None:
    if f.subject_type == "call":
        conn.execute("UPDATE pb_call_edition SET dirty_seq=dirty_seq+1 WHERE call_id=?", (f.subject_id,))


def append(conn: sqlite3.Connection, f: FactInput, now: str, historic: bool = False) -> AppendResult:
    """Append a fact following V-07/V-08. Same-source edits supersede; cross-source disagreements open a conflict."""
    with transaction(conn):
        active = conn.execute("SELECT * FROM pb_fact WHERE subject_id=? AND predicate=? AND scope_ref=? AND superseded_at IS NULL",
                              (f.subject_id, f.predicate, f.scope_ref)).fetchall()
        supersede: List[sqlite3.Row] = []
        conflicting: List[sqlite3.Row] = []
        action = "created"
        for a in active:
            if _same_value(a, f):
                if a["source_id"] == f.source_id:
                    if EPISTEMIC_RANK[f.epistemic] > EPISTEMIC_RANK[a["epistemic"]]:
                        supersede.append(a)
                        action = "promoted"
                    else:
                        rev = conn.execute("SELECT revision_id FROM pb_evidence WHERE evidence_id=?", (f.evidence_id,)).fetchone() \
                            if f.evidence_id else None
                        rev_id = rev["revision_id"] if rev else f.inspected_revision_id
                        if rev_id is None:
                            return AppendResult(a["fact_id"], "unchanged", [])
                        fresh = None
                        if a["epistemic"] == "verified_fact":
                            fresh = timeutil.iso(timeutil.parse_iso(f.collected_at) + dt.timedelta(days=freshness_days(f.predicate)))
                        conn.execute("INSERT INTO pb_fact_confirmation(confirmation_id,fact_id,revision_id,evidence_id,confirmed_at,fresh_until)"
                                     " VALUES(?,?,?,?,?,?)", (new_id("conf", _ms(now)), a["fact_id"], rev_id, f.evidence_id, f.collected_at, fresh))
                        return AppendResult(a["fact_id"], "unchanged", [])
                else:
                    action = "corroborated" if action == "created" else action
            else:
                if a["source_id"] == f.source_id:
                    supersede.append(a)
                    action = "superseded_prior"
                elif not _contradicts(a, f):
                    continue
                else:
                    conflicting.append(a)
        group_id = None
        demote: List[sqlite3.Row] = []
        if conflicting:
            lead = f.epistemic in ("unverified", "ai_inference") or any(c["epistemic"] in ("unverified", "ai_inference") for c in conflicting)
            kind = "lead_discrepancy" if lead else "value_conflict"
            row = conn.execute("SELECT group_id FROM pb_conflict_group WHERE subject_id=? AND predicate=? AND scope_ref=? AND resolved_at IS NULL "
                               "AND kind=? ORDER BY opened_at DESC LIMIT 1", (f.subject_id, f.predicate, f.scope_ref, kind)).fetchone()
            if row:
                group_id = row["group_id"]
            else:
                group_id = new_id("cg", _ms(now))
                conn.execute("INSERT INTO pb_conflict_group(group_id,subject_type,subject_id,predicate,scope_ref,kind,opened_at) VALUES(?,?,?,?,?,?,?)",
                             (group_id, f.subject_type, f.subject_id, f.predicate, f.scope_ref, kind, now))
            action = "conflict"
            if kind == "value_conflict":
                if f.epistemic == "verified_fact":
                    f = dataclasses.replace(f, epistemic="source_claim", verified_at=None, verifier=None, fresh_until=None)
                demote = [c for c in conflicting if c["epistemic"] == "verified_fact"]
        problems = validate_fact(conn, f, now, historic=historic)
        if problems:
            raise ValidationError(problems)
        fid = _insert(conn, f, now, group_id)
        superseded = []
        for old in supersede:
            _supersede(conn, old["fact_id"], fid, now)
            superseded.append(old["fact_id"])
        if group_id:
            for c in conflicting:
                if c["conflict_group_id"] is None:
                    conn.execute("UPDATE pb_fact SET conflict_group_id=? WHERE fact_id=?", (group_id, c["fact_id"]))
            for c in demote:  # append-only demotion: replace the verified fact with an unverified-by-conflict copy
                copy = _copy_as_claim(c, now, "conflict_opened")
                nid = _insert_copy(conn, copy, now, group_id)
                _supersede(conn, c["fact_id"], nid, now)
                superseded.append(c["fact_id"])
            _outbox(conn, "conflict_opened", f.subject_id, {"group_id": group_id, "predicate": f.predicate}, now)
        if superseded or action == "created":
            _outbox(conn, "fact_changed", f.subject_id, {"fact_id": fid, "predicate": f.predicate, "action": action}, now)
        _mark_call_dirty(conn, f)
        return AppendResult(fid, action, superseded, group_id)


def _copy_as_claim(row: sqlite3.Row, now: str, reason: str) -> FactInput:
    return FactInput(
        subject_type=row["subject_type"], subject_id=row["subject_id"], predicate=row["predicate"], source_id=row["source_id"],
        value_state=row["value_state"], epistemic="source_claim", collected_at=row["collected_at"], extractor=row["extractor"],
        extractor_version=row["extractor_version"], scope_ref=row["scope_ref"],
        value=json.loads(row["value_json"]) if row["value_json"] else None, value_raw=row["value_raw"],
        evidence_id=row["evidence_id"], inspected_revision_id=row["inspected_revision_id"],
        source_authority=row["source_authority"], effective_from=row["effective_from"], effective_to=row["effective_to"],
        correction_of=row["fact_id"], correction_reason=reason, run_id=row["run_id"])


def _insert_copy(conn: sqlite3.Connection, f: FactInput, now: str, group_id: Optional[str]) -> str:
    return _insert(conn, f, now, group_id)


def correct_fact(conn: sqlite3.Connection, old_fact_id: str, new: FactInput, reason: str, now: str) -> AppendResult:
    """Explicit correction: new fact points at the old one (correction_of), old is superseded. History is kept."""
    with transaction(conn):
        old = conn.execute("SELECT * FROM pb_fact WHERE fact_id=? AND superseded_at IS NULL", (old_fact_id,)).fetchone()
        if old is None:
            raise ValidationError([Violation("V-08", "can only correct a live fact")])
        new = dataclasses.replace(new, correction_of=old_fact_id, correction_reason=reason)
        problems = validate_fact(conn, new, now)
        if problems:
            raise ValidationError(problems)
        fid = _insert(conn, new, now, old["conflict_group_id"])
        _supersede(conn, old_fact_id, fid, now)
        _outbox(conn, "fact_corrected", new.subject_id, {"fact_id": fid, "old": old_fact_id, "reason": reason}, now)
        _mark_call_dirty(conn, new)
        return AppendResult(fid, "superseded_prior", [old_fact_id])


def resolve_conflict(conn: sqlite3.Connection, group_id: str, decision_id: str, keep_fact_id: Optional[str], now: str) -> None:
    """Only a recorded review decision can close a conflict; losers are superseded by the kept fact, never deleted."""
    with transaction(conn):
        if conn.execute("SELECT 1 FROM pb_review_decision WHERE decision_id=? AND kind='resolve_conflict'", (decision_id,)).fetchone() is None:
            raise ValidationError([Violation("V-07", "conflict resolution requires a resolve_conflict review decision")])
        members = conn.execute("SELECT fact_id FROM pb_fact WHERE conflict_group_id=? AND superseded_at IS NULL", (group_id,)).fetchall()
        if keep_fact_id is not None:
            if keep_fact_id not in [m["fact_id"] for m in members]:
                raise ValidationError([Violation("V-07", "kept fact is not a live member of the group")])
            for m in members:
                if m["fact_id"] != keep_fact_id:
                    _supersede(conn, m["fact_id"], keep_fact_id, now)
        conn.execute("UPDATE pb_conflict_group SET resolved_at=?, resolution_decision_id=? WHERE group_id=?", (now, decision_id, group_id))
        sub = conn.execute("SELECT subject_id FROM pb_conflict_group WHERE group_id=?", (group_id,)).fetchone()
        conn.execute("UPDATE pb_call_edition SET dirty_seq=dirty_seq+1 WHERE call_id=?", (sub["subject_id"],))


def resolve_noncontradicting_conflicts(conn: sqlite3.Connection, now: str) -> List[str]:
    """Close obsolete groups with an audited decision; never rewrite or promote facts.

    Historical fact links remain immutable. Readers must consult the group's
    resolved_at rather than treating every retained link as an open conflict.
    """
    resolved = []
    with transaction(conn):
        groups = conn.execute("SELECT * FROM pb_conflict_group WHERE resolved_at IS NULL ORDER BY group_id").fetchall()
        for group in groups:
            members = conn.execute("SELECT * FROM pb_fact WHERE conflict_group_id=? AND superseded_at IS NULL ORDER BY fact_id",
                                   (group["group_id"],)).fetchall()
            # A fact already linked to an older group cannot be relinked by append
            # (V-08). Include those active assertions so a later real disagreement
            # cannot be mistaken for a singleton group and auto-closed.
            active = conn.execute("SELECT * FROM pb_fact WHERE subject_id=? AND predicate=? AND scope_ref=? "
                                  "AND superseded_at IS NULL ORDER BY fact_id",
                                  (group["subject_id"], group["predicate"], group["scope_ref"])).fetchall()
            if any(_contradicts(a, b) for i, a in enumerate(active) for b in active[i + 1:]):
                continue
            reason = ("auto: non-committal mention does not contradict" if any(_non_committal(m) for m in members)
                      else "auto: no active cross-source contradiction")
            decision_id = stable_id("rd", "noncontradicting_conflict", group["group_id"])
            conn.execute("INSERT INTO pb_review_decision(decision_id,kind,actor,rationale,created_at,data) VALUES(?,?,?,?,?,?)",
                         (decision_id, "resolve_conflict", "system", reason, now,
                          dumps({"group_id": group["group_id"], "active_fact_ids": [m["fact_id"] for m in members]})))
            resolve_conflict(conn, group["group_id"], decision_id, None, now)
            if group["subject_type"] == "outlet":
                conn.execute("UPDATE pb_call_edition SET dirty_seq=dirty_seq+1 WHERE outlet_id=?", (group["subject_id"],))
            elif group["subject_type"] == "organizer":
                conn.execute("UPDATE pb_call_edition SET dirty_seq=dirty_seq+1 WHERE organizer_id=?", (group["subject_id"],))
            resolved.append(group["group_id"])
    return resolved


def effective_fresh_until(conn: sqlite3.Connection, fact_id: str) -> Optional[str]:
    row = conn.execute("SELECT fresh_until FROM pb_fact WHERE fact_id=?", (fact_id,)).fetchone()
    if row is None or row["fresh_until"] is None:
        return None
    best = row["fresh_until"]
    for c in conn.execute("SELECT fresh_until FROM pb_fact_confirmation WHERE fact_id=? AND fresh_until IS NOT NULL", (fact_id,)):
        if c["fresh_until"] > best:
            best = c["fresh_until"]
    return best


def live_facts(conn: sqlite3.Connection, subject_id: str) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM pb_fact WHERE subject_id=? AND superseded_at IS NULL ORDER BY predicate, recorded_at", (subject_id,)).fetchall()


def fact_history(conn: sqlite3.Connection, subject_id: str, predicate: str, scope_ref: Optional[str] = None) -> List[sqlite3.Row]:
    sql = "SELECT * FROM pb_fact WHERE subject_id=? AND predicate=?"
    args = [subject_id, predicate]
    if scope_ref:
        sql += " AND scope_ref=?"
        args.append(scope_ref)
    return conn.execute(sql + " ORDER BY rowid", args).fetchall()
