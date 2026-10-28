"""Metrics scoped to the stated target lists. Every number is computed from DB rows; nothing is estimated."""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, Optional

from .. import timeutil
from ..profiles import pitchboard as prof


def _scoped(svc, target_ids):
    return ",".join("'%s'" % t for t in target_ids)


def compute(svc: sqlite3.Connection, infra: sqlite3.Connection, targets: dict, gold: Optional[dict] = None) -> Dict:
    tids = [t["id"] for t in targets["targets"]]
    q = _scoped(svc, tids)
    out: Dict = {"target_lists": tids}
    # 1. new valid sources: registered, NOT in the seed list, and at least one non-stale call/rules page with extracted required fields
    per: Dict[str, Dict] = {}
    for t in tids:
        reg_new = svc.execute("SELECT count(*) FROM pb_source_site WHERE target_id=? AND in_seed_list=0", (t,)).fetchone()[0]
        valid = svc.execute(
            "SELECT count(DISTINCT ss.source_id) FROM pb_source_site ss JOIN pb_source s ON s.owner_outlet_id=ss.outlet_id "
            "JOIN pb_raw_ref r ON r.source_id=s.source_id AND r.outcome='ok' JOIN pb_label l ON l.raw_id=r.raw_id "
            "AND ((l.label='rules') OR (l.label='recruitment' AND l.status NOT IN ('stale_prior_year','results_not_call','past_deadline'))) "
            "WHERE ss.target_id=? AND ss.in_seed_list=0 AND EXISTS (SELECT 1 FROM pb_extraction e WHERE e.raw_id=r.raw_id AND e.field IN (%s))" %
            ",".join("'%s'" % k for k in prof.REQUIRED), (t,)).fetchone()[0]
        cand = dict(svc.execute("SELECT state, count(*) FROM pb_discovery_candidate WHERE target_id=? GROUP BY state", (t,)).fetchall())
        per[t] = {"candidates": sum(cand.values()), "by_state": cand, "new_sources_registered": reg_new, "new_valid_sources": valid}
    out["per_target"] = per
    out["new_valid_sources_total"] = sum(v["new_valid_sources"] for v in per.values())
    out["new_sources_registered_total"] = sum(v["new_sources_registered"] for v in per.values())
    ev = svc.execute("SELECT count(*) FROM pb_discovery_candidate WHERE state IN ('registered','deprioritized','rejected')").fetchone()[0]
    out["evaluation_precision_registered_over_evaluated"] = round(out["new_sources_registered_total"] / ev, 3) if ev else None
    # 2. new targets (outlets + call editions) from non-seed sites
    out["new_outlets"] = svc.execute("SELECT count(*) FROM pb_source_site WHERE in_seed_list=0 AND target_id IN (%s)" % q).fetchone()[0]
    out["new_call_editions"] = svc.execute("SELECT count(*) FROM pb_call_edition c JOIN pb_source_site ss ON ss.outlet_id=c.outlet_id WHERE ss.in_seed_list=0 AND ss.target_id IN (%s)" % q).fetchone()[0]
    # 3. required-field coverage (per outlet or call subject x predicate), live facts only
    cov = {"stated": 0, "explicitly_none": 0, "not_stated_in_inspected_scope": 0, "empty": 0}
    per_pred: Dict[str, Dict[str, int]] = {}
    sites = svc.execute("SELECT outlet_id FROM pb_source_site WHERE target_id IN (%s)" % q).fetchall()
    for s in sites:
        calls = [r[0] for r in svc.execute("SELECT call_id FROM pb_call_edition WHERE outlet_id=?", (s["outlet_id"],))]
        for pred, (stype, imp) in prof.REQUIRED.items():
            subs = calls if stype == "call" else [s["outlet_id"]]
            for sub in subs:
                f = svc.execute("SELECT value_state FROM pb_fact WHERE subject_id=? AND predicate=? AND superseded_at IS NULL ORDER BY rowid DESC LIMIT 1", (sub, pred)).fetchone()
                k = f["value_state"] if f and f["value_state"] in cov else "empty"
                cov[k] += 1
                per_pred.setdefault(pred, {"stated": 0, "explicitly_none": 0, "not_stated_in_inspected_scope": 0, "empty": 0})[k] += 1
    tot = sum(cov.values())
    out["required_field_cells"] = cov
    out["required_field_coverage_known"] = round((cov["stated"] + cov["explicitly_none"]) / tot, 3) if tot else None
    out["required_field_coverage_classified"] = round((tot - cov["empty"]) / tot, 3) if tot else None
    out["per_predicate"] = per_pred
    # 4. gaps
    g = dict(svc.execute("SELECT state, count(*) FROM pb_gap GROUP BY state").fetchall())
    gc = dict(svc.execute("SELECT gap_class, count(*) FROM pb_gap WHERE state='open' GROUP BY gap_class").fetchall())
    total = sum(g.values())
    out["gaps"] = {"by_state": g, "open_by_class": gc, "total": total,
                   "resolution_rate_filled": round(g.get("filled", 0) / total, 3) if total else None,
                   "resolution_rate_filled_or_classified": round((g.get("filled", 0) + g.get("closed_absent", 0) + g.get("blocked", 0)) / total, 3) if total else None,
                   "filled_by_gap_fill_search": svc.execute("SELECT count(*) FROM pb_gap WHERE state='filled' AND attempts>0").fetchone()[0]}
    # 5. change latency (seconds from fetch to service record) for facts that supersede an earlier one
    lat = [timeutil.iso_to_epoch(r["recorded_at"]) - timeutil.iso_to_epoch(r["collected_at"]) for r in
           svc.execute("SELECT f.recorded_at, f.collected_at FROM pb_fact f WHERE EXISTS (SELECT 1 FROM pb_fact o WHERE o.superseded_by=f.fact_id)")]
    out["change_latency_s"] = {"n": len(lat), "max": max(lat) if lat else None, "avg": round(sum(lat) / len(lat), 2) if lat else None,
                               "detection_bound_hours": "refresh_hours=24 per source (freshness budget)"}
    # 6. cost per valid fact
    verified = svc.execute("SELECT count(*) FROM pb_fact WHERE epistemic='verified_fact'").fetchone()[0]
    facts = svc.execute("SELECT count(*) FROM pb_fact WHERE value_state IN ('stated','explicitly_none')").fetchone()[0]
    rq = svc.execute("SELECT count(*) FROM pb_raw_ref").fetchone()[0]
    used = infra.execute("SELECT coalesce(sum(used),0) FROM budget_ledger WHERE resource='requests'").fetchone()[0]
    out["cost"] = {"verified_facts": verified, "stated_facts": facts, "raw_fetches_logged": rq, "network_requests_ledger": used, "model_cost_usd": 0.0,
                   "requests_per_verified_fact": round(used / verified, 2) if verified else None, "requests_per_stated_fact": round(used / facts, 2) if facts else None}
    out["alerts"] = dict(infra.execute("SELECT kind, count(*) FROM alert GROUP BY kind").fetchall())
    out["jobs"] = dict(infra.execute("SELECT state, count(*) FROM job GROUP BY state").fetchall())
    if gold:
        out["gold"] = gold
    return out
