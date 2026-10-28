"""Validation rules V-01..V-15 (docs/DESIGN.md section 2.4). Pure checks return Violations; stores enforce them."""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from . import timeutil
from .ids import sha256_hex
from .model import (AUTHORITIES, EPISTEMIC, EPISTEMIC_RANK, EXPLICIT_NONE_PATTERNS, EMAIL_RE, PHONE_RE, PREDICATES,
                    SUBJECT_TYPES, VALUE_STATES, FactInput, Violation, normalize_money)

MONEY_PREDICATES = ("fee.submission", "compensation.rate")
DATE_PREDICATES = ("deadline.opens_on", "deadline.closes_on")


def sanitize_excerpt(text: str) -> str:
    """Redact personal contact details before anything is persisted (V-10). Redaction, then hash of the redacted text."""
    text = EMAIL_RE.sub("[email]", text)
    text = PHONE_RE.sub("[phone]", text)
    return " ".join(text.split())[:280]


def check_excerpt(text: str) -> List[Violation]:
    out = []
    if not text or len(text) > 280:
        out.append(Violation("V-10", "excerpt must be 1..280 chars"))
    if EMAIL_RE.search(text) or PHONE_RE.search(text):
        out.append(Violation("V-10", "excerpt contains contact details"))
    return out


def _get_evidence(conn: sqlite3.Connection, evidence_id: Optional[str]):
    if not evidence_id:
        return None
    return conn.execute("SELECT * FROM pb_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()


def validate_fact(conn: sqlite3.Connection, f: FactInput, now: str, historic: bool = False) -> List[Violation]:
    v: List[Violation] = []
    # --- V-01 required fields / vocabulary
    if f.subject_type not in SUBJECT_TYPES:
        v.append(Violation("V-01", "bad subject_type"))
    if f.predicate not in PREDICATES:
        v.append(Violation("V-01", "unknown predicate %r" % f.predicate))
    if f.value_state not in VALUE_STATES:
        v.append(Violation("V-01", "bad value_state"))
    if f.epistemic not in EPISTEMIC:
        v.append(Violation("V-01", "bad epistemic"))
    if f.source_authority not in AUTHORITIES:
        v.append(Violation("V-01", "bad source_authority"))
    for name in ("collected_at",):
        try:
            timeutil.parse_iso(getattr(f, name))
        except ValueError:
            v.append(Violation("V-01", "%s must be UTC ISO-8601" % name))
    if v:
        return v
    ev = _get_evidence(conn, f.evidence_id)
    if f.value_state in ("stated", "explicitly_none") and ev is None:
        v.append(Violation("V-01", "stated/explicitly_none requires existing evidence"))
    if f.value_state == "not_stated_in_inspected_scope" and not f.inspected_revision_id:
        v.append(Violation("V-01", "not_stated requires the inspected revision"))
    if f.value_state in ("stated", "explicitly_none") and f.value is None:
        v.append(Violation("V-01", "value required"))
    # --- V-02 verified_fact conditions
    if f.epistemic == "verified_fact":
        if f.source_authority != "official_primary":
            v.append(Violation("V-02", "verified_fact needs official_primary source"))
        if not (f.verifier and f.verified_at and f.fresh_until):
            v.append(Violation("V-02", "verified_fact needs verifier, verified_at, fresh_until"))
        if ev is not None and sha256_hex(ev["excerpt"]) != ev["quote_hash"]:
            v.append(Violation("V-02", "evidence excerpt hash mismatch"))
        if f.value_state not in ("stated", "explicitly_none"):
            v.append(Violation("V-02", "verified_fact must carry a stated value"))
    # --- V-03 explicit-none needs explicit wording
    if f.value_state == "explicitly_none":
        pat = EXPLICIT_NONE_PATTERNS.get(f.predicate)
        if pat is None or ev is None or not pat.search(ev["excerpt"]):
            v.append(Violation("V-03", "explicitly_none requires explicit 'free/none/worldwide' wording in evidence"))
    # --- V-04 scope isolation
    if conn.execute("SELECT 1 FROM pb_scope WHERE scope_ref=?", (f.scope_ref,)).fetchone() is None:
        v.append(Violation("V-04", "unknown scope_ref %r" % f.scope_ref))
    # --- V-05 money comparability
    if f.predicate in MONEY_PREDICATES and f.value_state == "stated" and isinstance(f.value, dict) \
            and "state" not in f.value:
        want = normalize_money(f.value)["comparable"]
        if bool(f.value.get("comparable")) != want:
            v.append(Violation("V-05", "value.comparable must be %s (currency+unit+amount known?)" % want))
    # --- V-06 dates must be unambiguous ISO dates
    if f.predicate in DATE_PREDICATES and f.value_state == "stated":
        value = f.value or {}
        date = value.get("date")
        try:
            timeutil.parse_date(date)
        except ValueError:
            v.append(Violation("V-06", "date must be an unambiguous YYYY-MM-DD"))
        if f.predicate == "deadline.closes_on" and value.get("tz_source") not in ("explicit", "unknown"):
            v.append(Violation("V-06", "closes_on requires tz_source explicit|unknown"))
        if value.get("tz_source") == "explicit" and value.get("utc_offset_hours") is None:
            v.append(Violation("V-06", "explicit tz requires utc_offset_hours"))
    # --- V-09 model output never exceeds ai_inference
    if f.extractor.startswith("ai:") and EPISTEMIC_RANK[f.epistemic] > EPISTEMIC_RANK["ai_inference"]:
        v.append(Violation("V-09", "AI/heuristic-made values cap at ai_inference"))
    # --- V-10 evidence hygiene
    if ev is not None:
        v.extend(check_excerpt(ev["excerpt"]))
    if f.value_raw is not None:
        v.extend(Violation("V-10", m.message) for m in check_excerpt(f.value_raw))
    # --- V-13 source permission and epistemic ceiling
    src = conn.execute("SELECT permission, max_epistemic FROM pb_source WHERE source_id=?", (f.source_id,)).fetchone()
    if src is None:
        v.append(Violation("V-13", "unregistered source"))
    elif not historic:  # historic = backfill of facts observed before the permission review; never for new collection
        if src["permission"] in ("not_collectable", "pending_review"):
            v.append(Violation("V-13", "source permission is %s: new facts not accepted" % src["permission"]))
        if EPISTEMIC_RANK[f.epistemic] > EPISTEMIC_RANK[src["max_epistemic"]]:
            v.append(Violation("V-13", "source ceiling is %s" % src["max_epistemic"]))
    # --- V-11 deadline ordering against an already-active sibling
    if f.predicate in DATE_PREDICATES and f.value_state == "stated" and f.subject_type == "call":
        other = "deadline.opens_on" if f.predicate == "deadline.closes_on" else "deadline.closes_on"
        row = conn.execute("SELECT value_json FROM pb_fact WHERE subject_id=? AND predicate=? AND scope_ref=? "
                           "AND superseded_at IS NULL AND source_id=? ORDER BY recorded_at DESC LIMIT 1",
                           (f.subject_id, other, f.scope_ref, f.source_id)).fetchone()
        if row and row["value_json"]:
            ov = json.loads(row["value_json"]).get("date")
            mine = (f.value or {}).get("date")
            if ov and mine:
                opens, closes = (mine, ov) if f.predicate == "deadline.opens_on" else (ov, mine)
                if closes < opens:
                    v.append(Violation("V-11", "closes_on before opens_on"))
    return v
