"""Representative questions (docs/QUESTIONS.md) answered from the service projection only.
Three-valued: yes / no / unknown; unknown is never folded into yes."""
from __future__ import annotations

import json
from typing import Dict, List


def _rows(svc) -> List[dict]:
    return [json.loads(r[0]) for r in svc.execute("SELECT payload FROM pb_search_projection ORDER BY call_id")]


def _cell(p, pred):
    return p["conditions"][pred]


def bucket(p: dict, required: List[str]) -> str:
    if p["applicable_now"] not in ("yes", "maybe"):
        return "부적합/마감"
    cells = [_cell(p, r) for r in required]
    if any(c["answer"] == "conflict" for c in cells):
        return "검수 필요(충돌)"
    if all(c["answer"] in ("stated", "explicitly_none") and all(f["epistemic"] == "verified_fact" and not f["stale"] for f in c["facts"]) for c in cells) \
            and p["applicable_now"] == "yes":
        return "확정 적합"
    return "적합 가능(미확인 있음)"


def answer_all(svc) -> Dict[str, object]:
    rows = _rows(svc)
    def brief(p, extra=None):
        d = {"call": p["call_id"], "owner": p["owner"]["name"], "status": p["status"], "closes_on": p["closes_on"], "applicable_now": p["applicable_now"]}
        d.update(extra or {})
        return d
    q1 = [brief(p, {"bucket": bucket(p, ["deadline.closes_on"])}) for p in rows if p["applicable_now"] in ("yes", "maybe")]
    def fee_answer(p):
        c = _cell(p, "fee.submission")
        if c["answer"] == "explicitly_none":
            return "yes"
        if c["answer"] == "stated":
            v = (c["facts"][0]["value"] or {})
            return "no" if (v.get("amount") or 0) > 0 else "unknown"
        return "unknown"
    q3 = [brief(p, {"no_fee": fee_answer(p)}) for p in rows if p["applicable_now"] in ("yes", "maybe")]
    def region(p):
        c = _cell(p, "eligibility.region")
        if c["answer"] == "explicitly_none" and (c["facts"][0]["value"] or {}).get("scope") == "worldwide":
            return "yes"
        if c["answer"] == "stated":
            v = c["facts"][0]["value"] or {}
            k = v.get("korea_resident_eligible", True if v.get("worldwide") else None)
            if k is True:
                return "yes"
            if k is False:
                return "no"
        return "unknown" if c["answer"] in ("unknown",) else ("conflict" if c["answer"] == "conflict" else "unknown_needs_reading")
    q5 = [brief(p, {"korea_resident_eligible": region(p)}) for p in rows if p["applicable_now"] in ("yes", "maybe")]
    q6 = [brief(p, {"history": p["status_history"]}) for p in rows if any(h["to_status"] in ("extended", "cancelled") or h["kind"] != "transition" for h in p["status_history"])]
    q8 = [brief(p, {"stale": True}) for p in rows if p["closes_on"] and p["applicable_now"] in ("yes", "maybe", "unknown")
          and any(f["stale"] for f in _cell(p, "deadline.closes_on")["facts"])]
    q10 = sum(1 for p in rows for c in p["conditions"].values() for f in c["facts"] if f["evidence"] is None and f["value_state"] in ("stated", "explicitly_none"))
    return {"Q1_open_now": q1, "Q3_no_fee": q3, "Q5_korea_eligible": q5, "Q6_changed_extended_cancelled": q6,
            "Q8_closing_but_stale": q8, "Q10_stated_values_without_evidence": q10, "projection_rows": len(rows)}
