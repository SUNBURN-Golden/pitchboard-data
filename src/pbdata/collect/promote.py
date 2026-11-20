"""Layer 2/3 -> layer 4: promote extraction rows into facts and call editions (rules only; AI cap 0)."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
from typing import Dict, List, Optional

from .. import facts, statemachine, timeutil
from ..ids import new_id
from ..model import FactInput, ValidationError, freshness_days
from ..profiles import pitchboard as prof
from . import store
from ..collect.parse import Doc, Segment

CALL_PREDICATES = {"deadline.closes_on", "deadline.opens_on", "status.declared"}
SKIP_LABEL_STATUS = {"stale_prior_year", "results_not_call", "past_deadline", "not_writing_call"}


@dataclasses.dataclass
class PromoteStats:
    facts: int = 0
    verified: int = 0
    rejected: Dict[str, int] = dataclasses.field(default_factory=dict)
    calls_created: int = 0
    skipped_label: str = ""
    superseded: int = 0
    conflicts: int = 0
    withdrawn: int = 0
    fact_ids: List[str] = dataclasses.field(default_factory=list)
    observed_fact_ids: List[str] = dataclasses.field(default_factory=list)

    def reject(self, code: str) -> None:
        self.rejected[code] = self.rejected.get(code, 0) + 1


def reparse_confirms(ext: sqlite3.Row, ocr: bool) -> bool:
    """Deterministic verifier: re-run the rules on the stored 280-char excerpt only; the value must reproduce."""
    if ocr:
        return False
    d = Doc("html", segments=[Segment(0, "p", ext["excerpt"])])
    want = json.loads(ext["value_json"]) if ext["value_json"] else None
    for e in prof.extract(d):
        if e.field == ext["field"] and e.value_state == ext["value_state"] and e.value == want:
            return True
    return False


def _call(svc, outlet_id: str, key: str, scope: str, title: str, now: str, stats: PromoteStats) -> str:
    row = svc.execute("SELECT call_id FROM pb_call_edition WHERE outlet_id=? AND edition_key=? AND scope_ref=?", (outlet_id, key, scope)).fetchone()
    if row:
        return row["call_id"]
    cid = new_id("call", int(timeutil.parse_iso(now).timestamp() * 1000))
    svc.execute("INSERT INTO pb_call_edition(call_id,outlet_id,edition_key,scope_ref,title_raw,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (cid, outlet_id, key, scope, title[:200], "candidate", now))
    stats.calls_created += 1
    return cid


def _continuing_call(svc, outlet_id: str, source_id: str, today: str):
    rows = svc.execute("SELECT DISTINCT c.call_id, json_extract(f.value_json,'$.date') d FROM pb_fact f JOIN pb_call_edition c ON c.call_id=f.subject_id "
                       "WHERE f.source_id=? AND f.predicate='deadline.closes_on' AND f.subject_type='call' AND f.superseded_at IS NULL AND c.outlet_id=? "
                       "AND c.status IN ('candidate','announced','open','extended')", (source_id, outlet_id)).fetchall()
    live = [r for r in rows if r["d"] and r["d"] >= today]
    return live[0]["call_id"] if len(live) == 1 and len(rows) == 1 else None


def promote_raw(svc: sqlite3.Connection, raw_id: str, now: str, *, verified_only=False, complete=True) -> PromoteStats:
    stats = PromoteStats()
    raw = svc.execute("SELECT * FROM pb_raw_ref WHERE raw_id=?", (raw_id,)).fetchone()
    if raw is None or raw["outcome"] != "ok" or raw["kind"] not in ("html", "pdf", "image"):
        stats.skipped_label = "not_promotable"
        return stats
    src = svc.execute("SELECT * FROM pb_source WHERE source_id=?", (raw["source_id"],)).fetchone()
    if src["permission"] not in ("allowed_public", "allowed_conditional") or not src["owner_outlet_id"]:
        stats.skipped_label = "permission"
        return stats
    if verified_only and (src["authority"] != "official_primary" or src["max_epistemic"] != "verified_fact"):
        stats.skipped_label = "source_ceiling"
        return stats

    def withdraw(kept_dates):
        if complete:
            stats.withdrawn = withdraw_unstated_deadlines(svc, raw_id, kept_dates, now)

    labels = svc.execute("SELECT * FROM pb_label WHERE raw_id=? AND labeler_version=?", (raw_id, prof.LABELER_VERSION)).fetchall()
    rec = next((l for l in labels if l["label"] == "recruitment"), None)
    has_rules = any(l["label"] == "rules" for l in labels)
    if rec is not None and rec["status"] in SKIP_LABEL_STATUS and not has_rules:
        stats.skipped_label = rec["status"]            # last year's call / results announcement: never an open call
        withdraw(set())
        return stats
    if rec is None and not has_rules:
        stats.skipped_label = "not_call_or_rules"
        withdraw(set())
        return stats
    if rec is not None and rec["status"] in SKIP_LABEL_STATUS:
        stats.skipped_label = rec["status"]            # rules page with stale call markers: keep only generic rules, no calls
    exts = svc.execute("SELECT * FROM pb_extraction WHERE raw_id=? AND extractor_version=? AND field NOT LIKE 'signal.%'", (raw_id, prof.EXTRACTOR_VERSION)).fetchall()
    rev = svc.execute("SELECT revision_id FROM pb_source_revision WHERE source_id=? AND content_hash=?", (raw["source_id"], raw["text_hash"])).fetchone()
    if rev is None or not exts:
        withdraw(set())
        return stats
    ocr = exts[0]["extractor"].startswith("ocr:")

    def verifies(e):
        return (e["confidence"] >= 0.8 and e["value_state"] in ("stated", "explicitly_none") and reparse_confirms(e, ocr)
                and src["authority"] == "official_primary" and e["field"] != "policy.ai")

    observed_closes = set()
    if verified_only:
        observed_closes = {json.loads(e["value_json"])["date"] for e in exts if e["field"] == "deadline.closes_on"}
        exts = [e for e in exts if e["extractor"] == "rules" and verifies(e)]
    outlet = src["owner_outlet_id"]
    today = now[:10]
    title = svc.execute("SELECT value_json FROM pb_extraction WHERE raw_id=? AND field='signal.title'", (raw_id,)).fetchone()
    scope = "unspecified"
    # --- calls from deadline windows
    closes = [e for e in exts if e["field"] == "deadline.closes_on"]
    calls: Dict[str, str] = {}
    stale_closes = skip_calls = rec is not None and rec["status"] in SKIP_LABEL_STATUS
    future = [e for e in closes if json.loads(e["value_json"])["date"] >= today]
    reuse = _continuing_call(svc, outlet, raw["source_id"], today) if (len(future) == 1 and not skip_calls) else None
    for e in closes:
        date = json.loads(e["value_json"])["date"]
        if skip_calls or date < today:
            continue
        # a single-deadline page whose live call has not closed yet is the SAME call: a changed date is a correction/extension, not a new call
        calls[date] = reuse or _call(svc, outlet, date, scope, "call closing " + date, now, stats)
    declared = [e for e in exts if e["field"] == "status.declared"]
    if not calls and not skip_calls and any(json.loads(e["value_json"])["declared"] == "standing" for e in declared):
        calls["standing"] = _call(svc, outlet, "standing", scope, "standing submissions", now, stats)
    primary = calls.get(max(c for c in calls if c != "standing")) if any(c != "standing" for c in calls) else (calls.get("standing"))

    def make(e: sqlite3.Row, subject_type: str, subject_id: str):
        ev = facts.add_evidence(svc, rev["revision_id"], raw["source_id"], e["excerpt"], json.loads(e["locator"]).get("heading", ""),
                                int(json.loads(e["locator"]).get("seg", 0)) + 100000 * int(json.loads(e["locator"]).get("page", 0)), 0, now)
        value = json.loads(e["value_json"]) if e["value_json"] else None
        verified = verified_only or verifies(e)
        kw = {}
        if verified:
            kw = dict(verifier="rules:reparse-1", verified_at=now, fresh_until=timeutil.iso(timeutil.parse_iso(now) + dt.timedelta(days=freshness_days(e["field"]))))
        return FactInput(subject_type, subject_id, e["field"], raw["source_id"], e["value_state"],
                         "verified_fact" if verified else "source_claim", now, e["extractor"], e["extractor_version"], scope_ref=scope,
                         value=value, evidence_id=ev, inspected_revision_id=rev["revision_id"], source_authority=src["authority"], **kw), ev

    for e in exts:
        if e["field"] in ("signal.cue",):
            continue
        if e["field"] in CALL_PREDICATES:
            if skip_calls:
                continue
            if e["field"] == "status.declared":
                subj = primary
            else:
                date = json.loads(e["value_json"])["date"]
                if e["field"] == "deadline.closes_on":
                    subj = calls.get(date)
                else:  # opens_on: attach to the call whose closes_on is in the same segment
                    seg = json.loads(e["locator"]).get("seg")
                    match = [c for c in closes if json.loads(c["locator"]).get("seg") == seg and json.loads(c["locator"]).get("page") == json.loads(e["locator"]).get("page")]
                    subj = calls.get(json.loads(match[0]["value_json"])["date"]) if match else None
            if not subj:
                continue
            stype, sid = "call", subj
        else:
            if rec is not None and rec["status"] in SKIP_LABEL_STATUS:
                continue
            stype, sid = "outlet", outlet
        try:
            fi, ev = make(e, stype, sid)
            r = facts.append(svc, fi, now)
        except ValidationError as ex:
            for v in ex.violations:
                stats.reject(v.code)
            continue
        stats.observed_fact_ids.append(r.fact_id)
        if r.action != "unchanged":
            stats.facts += 1
            stats.fact_ids.append(r.fact_id)
        if fi.epistemic == "verified_fact" and r.action == "created":
            stats.verified += 1
        if r.action == "superseded_prior":
            stats.superseded += 1
            if stype == "call" and e["field"] == "deadline.closes_on" and _later_than_prior(svc, r.fact_id):
                _maybe_extend(svc, sid, ev, now)
        if r.action == "conflict":
            stats.conflicts += 1
    # --- state derivation (evidence-backed only)
    for key, cid in calls.items():
        _derive_status(svc, cid, key, exts, declared, now, raw, rev["revision_id"])
    kept_dates = {d for d in calls if d != "standing"}
    if verified_only and not skip_calls:
        # Evidence below the verification threshold is insufficient to replace
        # an existing date, but it is still a statement of that date.
        kept_dates.update(observed_closes)
    withdraw(kept_dates)
    return stats


WITHDRAW_REASON = "no_longer_stated_by_source"


def withdraw_unstated_deadlines(svc: sqlite3.Connection, raw_id: str, kept_dates, now: str) -> int:
    """A source's newest rules inspection is what that source states now. A live future call deadline the same source
    stated earlier but no longer yields (the page was edited, or current rules read it as a non-writing notice such as a
    membership drive) is withdrawn: superseded by a not_stated_in_inspected_scope fact for the inspected revision, with
    history kept. Deadlines from other sources are never touched; a later re-extraction that states it again restores it.
    Rules only: runs only when this raw was inspected by the current extractor/labeler, never on fetch errors."""
    raw = svc.execute("SELECT * FROM pb_raw_ref WHERE raw_id=?", (raw_id,)).fetchone()
    if raw is None or raw["outcome"] != "ok" or raw["kind"] not in ("html", "pdf", "image"):
        return 0
    inspected = svc.execute("SELECT 1 FROM pb_extraction WHERE raw_id=? AND extractor_version=? LIMIT 1", (raw_id, prof.EXTRACTOR_VERSION)).fetchone() \
        or svc.execute("SELECT 1 FROM pb_label WHERE raw_id=? AND labeler_version=? LIMIT 1", (raw_id, prof.LABELER_VERSION)).fetchone()
    rev = svc.execute("SELECT revision_id FROM pb_source_revision WHERE source_id=? AND content_hash=?", (raw["source_id"], raw["text_hash"])).fetchone()
    if not inspected or rev is None:
        return 0
    src = svc.execute("SELECT authority FROM pb_source WHERE source_id=?", (raw["source_id"],)).fetchone()
    rows = svc.execute("SELECT * FROM pb_fact WHERE source_id=? AND predicate='deadline.closes_on' AND subject_type='call' AND superseded_at IS NULL "
                       "AND value_state='stated' AND json_extract(value_json,'$.date')>=? AND collected_at<=?",
                       (raw["source_id"], now[:10], raw["fetched_at"])).fetchall()
    n = 0
    for f in rows:
        if json.loads(f["value_json"])["date"] in kept_dates:
            continue
        new = FactInput("call", f["subject_id"], "deadline.closes_on", raw["source_id"], "not_stated_in_inspected_scope", "source_claim", now,
                        "rules", prof.EXTRACTOR_VERSION, scope_ref=f["scope_ref"], inspected_revision_id=rev["revision_id"],
                        source_authority=src["authority"] if src else "unknown")
        try:
            facts.correct_fact(svc, f["fact_id"], new, WITHDRAW_REASON, now)
            n += 1
        except ValidationError:
            continue
    return n


def _later_than_prior(svc, fact_id: str) -> bool:
    row = svc.execute("SELECT json_extract(n.value_json,'$.date') nd, json_extract(o.value_json,'$.date') od FROM pb_fact n JOIN pb_fact o ON o.superseded_by=n.fact_id WHERE n.fact_id=?", (fact_id,)).fetchone()
    return bool(row and row["nd"] and row["od"] and row["nd"] > row["od"])


def _maybe_extend(svc, call_id, ev, now):
    st = svc.execute("SELECT status FROM pb_call_edition WHERE call_id=?", (call_id,)).fetchone()["status"]
    if st in ("open", "extended"):
        try:
            statemachine.transition(svc, call_id, "extended", "evidence", now, "collector", "deadline value superseded by later date", evidence_id=ev)
        except ValidationError:
            pass


def _derive_status(svc, call_id, key, exts, declared, now, raw, revision_id) -> None:
    cur = svc.execute("SELECT status FROM pb_call_edition WHERE call_id=?", (call_id,)).fetchone()["status"]
    if cur != "candidate":
        return
    today = now[:10]
    def ev_for(field, match=None):
        for e in exts:
            if e["field"] == field and (match is None or match(e)):
                loc = json.loads(e["locator"])
                return svc.execute("SELECT evidence_id FROM pb_evidence WHERE revision_id=? AND quote_hash=?", (revision_id, __import__("pbdata.ids", fromlist=["x"]).sha256_hex(e["excerpt"]))).fetchone()
        return None
    to, ev = None, None
    decl = {json.loads(e["value_json"])["declared"] for e in declared}
    if key == "standing":
        to, ev = "standing", ev_for("status.declared")
    else:
        closes_ev = ev_for("deadline.closes_on", lambda e: json.loads(e["value_json"])["date"] == key)
        opens = [json.loads(e["value_json"])["date"] for e in exts if e["field"] == "deadline.opens_on"]
        if "open" in decl and ev_for("status.declared"):
            to, ev = "open", ev_for("status.declared")
        elif opens and min(opens) <= today:
            to, ev = "open", closes_ev
        else:
            to, ev = "announced", closes_ev
    if to and ev:
        try:
            statemachine.transition(svc, call_id, to, "evidence", now, "collector", "derived from stated dates/declaration", evidence_id=ev["evidence_id"])
        except ValidationError:
            pass
