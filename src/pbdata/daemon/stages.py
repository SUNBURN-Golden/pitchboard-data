"""The nine daemon stages (docs/DESIGN.md section 4). Each takes a RunContext and returns a small JSON-able dict
that is stored as the stage checkpoint (infra.stage_run.output_digest)."""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Dict, List

from .. import timeutil
from ..ids import sha256_hex, stable_id
from ..model import ValidationError
from .. import statemachine
from ..collect import collector, promote, gaps, store
from ..collect.resolve import same_entity
from ..profiles import pitchboard as prof
from . import clips, project
from .runner import Deferred, NonRetryable, RunContext


def _since(c: RunContext) -> str:
    """Window start for 'what changed in this run'. Persisted in the discovery checkpoint so resume keeps the same window."""
    return c.state.get("discovery", {}).get("window_start") or c.opts.get("window_start") or c.clock()


def _prev_run_start(c: RunContext):
    """Start of the previous successful daemon run. The 04:20 collector fetches (and uses the day's request budget)
    before the daemon runs, so a window that starts at this run's own clock never sees those revisions and every run
    looks empty. Starting the window at the previous daemon run covers everything fetched since then (stages 3-4 are
    idempotent, so overlap is harmless)."""
    r = c.infra.execute("SELECT started_at FROM runs WHERE run_id LIKE 'dmn_%' AND run_id<>? AND outcome='ok' "
                        "ORDER BY started_at DESC LIMIT 1", (c.run_id,)).fetchone()
    return r[0] if r else None


def _collector_ctx(c: RunContext):
    if "_cctx" not in c.opts:
        from ..collect.net import Fetcher
        from ..collect.wiki import Api
        fetcher = c.opts.get("fetcher") or Fetcher(c.infra)
        api = c.opts.get("api") or Api(cap=c.opts.get("api_cap", 90))
        c.opts["_cctx"] = collector.Ctx(c.svc, c.infra, fetcher, api, c.opts["targets"], c.run_id, c.opts.get("run_tag", c.run_id),
                                        c.clock, c.opts.get("caps"))
    return c.opts["_cctx"]


# 1 ---------------------------------------------------------------- discovery
def s_discovery(c: RunContext) -> dict:
    out = {"window_start": c.opts.get("window_start") or _prev_run_start(c) or c.clock(), "discover_jobs": 0}
    if c.opts.get("offline"):
        return out
    out["discover_jobs"] = collector.seed_discovery(_collector_ctx(c))
    return out


# 2 ---------------------------------------------------------------- incremental fetch (+ manual clips)
def s_incremental_fetch(c: RunContext) -> dict:
    out = {"clips": clips.ingest_dir(c.svc, c.root, c.clock())}
    if c.opts.get("offline"):
        return out
    cctx = _collector_ctx(c)
    out["refresh_jobs"] = collector.enqueue_refresh(cctx, limit=c.opts.get("refresh_limit", 40))
    before = cctx.fetcher.requests + cctx.api.calls
    stats = collector.run(cctx, c.opts.get("max_jobs"), log=None if c.opts.get("quiet") else print)
    used = cctx.fetcher.requests + cctx.api.calls - before
    c.ledger.charge("http_requests", 0)            # cap check happens inside the collector's per-class budgets
    c.infra.execute("INSERT INTO cost_ledger(day,resource,amount) VALUES(?,?,?) ON CONFLICT(day,resource) DO UPDATE SET amount=amount+excluded.amount",
                    (c.ledger.day, "http_requests", used))
    out.update({"requests": used, "jobs_done": stats.get("jobs_done", 0), "pages_ok": stats.get("pages_ok", 0),
                "facts": stats.get("facts", 0), "verified_facts": stats.get("verified_facts", 0)})
    pending = c.infra.execute("SELECT count(*) FROM job WHERE state IN ('pending','retry_wait')").fetchone()[0]
    out["jobs_pending_after"] = pending
    return out


# 3 ---------------------------------------------------------------- change detection
def s_change_detection(c: RunContext) -> dict:
    since = _since(c)
    revs = c.svc.execute("SELECT revision_id, source_id, prev_revision_id, change_state, changed_segments, fetched_at FROM pb_source_revision "
                         "WHERE fetched_at>=? ORDER BY fetched_at", (since,)).fetchall()
    emitted = 0
    for r in revs:
        eid = "evt_chg_" + sha256_hex("%s|%s" % (r["prev_revision_id"], r["revision_id"]))[:24]
        cur = c.svc.execute("INSERT OR IGNORE INTO pb_outbox(event_id,kind,subject_id,payload,created_at) VALUES(?,?,?,?,?)",
                            (eid, "source_changed", r["source_id"], json.dumps({"old_rev": r["prev_revision_id"], "new_rev": r["revision_id"],
                             "change_state": r["change_state"], "changed_segments": json.loads(r["changed_segments"])[:50]}), c.clock()))
        emitted += cur.rowcount
    unchanged = c.svc.execute("SELECT count(*) FROM pb_raw_ref WHERE fetched_at>=? AND change_state='unchanged'", (since,)).fetchone()[0]
    return {"new_or_changed_revisions": len(revs), "changed": sum(1 for r in revs if r["change_state"] == "changed"),
            "new": sum(1 for r in revs if r["change_state"] == "new"), "unchanged_fetches": unchanged, "events_emitted": emitted}


# 4 ---------------------------------------------------------------- extraction (rules only; AI off, cap 0)
def s_extraction(c: RunContext) -> dict:
    if c.opts.get("ai_extraction"):
        c.ledger.charge("model_calls", 1)        # cap is 0 -> CostCapExceeded -> failure queue; never silently spends
    since = _since(c)
    raws = c.svc.execute("SELECT raw_id, url FROM pb_raw_ref WHERE fetched_at>=? AND outcome='ok' AND change_state!='unchanged' "
                         "AND kind IN ('html','pdf','image')", (since,)).fetchall()
    missing, promoted, facts_n = [], 0, 0
    for r in raws:
        n = c.svc.execute("SELECT count(*) FROM pb_extraction WHERE raw_id=? AND extractor_version=?", (r["raw_id"], prof.EXTRACTOR_VERSION)).fetchone()[0]
        if n == 0:
            missing.append(r["url"])
            continue
        ps = promote.promote_raw(c.svc, r["raw_id"], c.clock())   # idempotent: same value+source+evidence is a no-op
        promoted += 1
        facts_n += ps.facts
    return {"changed_raws": len(raws), "promoted": promoted, "new_facts": facts_n, "raws_without_extraction": len(missing),
            "ai_model_calls": 0}


# 5 ---------------------------------------------------------------- entity resolution (proposals only, never auto-merge)
def s_entity_resolution(c: RunContext) -> dict:
    outs = c.svc.execute("SELECT o.outlet_id, o.canonical_name, coalesce(s.host,'') host FROM pb_outlet o LEFT JOIN pb_source_site ss ON ss.outlet_id=o.outlet_id "
                         "LEFT JOIN pb_source s ON s.source_id=ss.source_id WHERE o.retired_to IS NULL").fetchall()
    proposed = 0
    for i in range(len(outs)):
        for j in range(i + 1, len(outs)):
            a, b = outs[i], outs[j]
            if a["outlet_id"] == b["outlet_id"]:
                continue
            if same_entity(a["canonical_name"], a["host"], b["canonical_name"], b["host"]):
                l, r = sorted([a["outlet_id"], b["outlet_id"]])
                cur = c.svc.execute("INSERT OR IGNORE INTO pb_merge_candidate(candidate_id,subject_type,left_id,right_id,reason,created_at) VALUES(?,?,?,?,?,?)",
                                    (stable_id("mc", l, r), "outlet", l, r, "rules:same_entity(name/host)", c.clock()))
                proposed += cur.rowcount
    # call editions of one outlet with the same close date under different keys -> review, not merge
    dup = c.svc.execute("SELECT a.call_id l, b.call_id r FROM pb_call_edition a JOIN pb_call_edition b ON a.outlet_id=b.outlet_id AND a.call_id<b.call_id "
                        "AND a.scope_ref=b.scope_ref AND a.title_raw=b.title_raw AND a.status NOT IN ('superseded','archived') AND b.status NOT IN ('superseded','archived')").fetchall()
    for d in dup:
        cur = c.svc.execute("INSERT OR IGNORE INTO pb_merge_candidate(candidate_id,subject_type,left_id,right_id,reason,created_at) VALUES(?,?,?,?,?,?)",
                            (stable_id("mc", d["l"], d["r"]), "call", d["l"], d["r"], "rules:same outlet+scope+title", c.clock()))
        proposed += cur.rowcount
    pending = c.svc.execute("SELECT count(*) FROM pb_merge_candidate WHERE state='needs_review'").fetchone()[0]
    return {"outlets_checked": len(outs), "merge_candidates_new": proposed, "merge_candidates_pending_review": pending, "auto_merged": 0}


# 6 ---------------------------------------------------------------- evidence verification
def s_evidence_verification(c: RunContext) -> dict:
    now = c.clock()
    rows = c.svc.execute("SELECT f.fact_id, f.epistemic, f.value_state, f.evidence_id, f.source_authority, f.fresh_until, f.extractor, "
                         "e.excerpt, e.quote_hash, e.source_id ev_src, f.source_id FROM pb_fact f LEFT JOIN pb_evidence e ON e.evidence_id=f.evidence_id "
                         "WHERE f.superseded_at IS NULL").fetchall()
    bad: Dict[str, List[str]] = {"hash_mismatch": [], "verified_without_evidence": [], "evidence_source_mismatch": [],
                                 "ai_as_fact": [], "stated_without_evidence": []}
    stale_verified = 0
    for r in rows:
        if r["evidence_id"] and r["excerpt"] is not None and sha256_hex(r["excerpt"]) != r["quote_hash"]:
            bad["hash_mismatch"].append(r["fact_id"])
        if r["epistemic"] == "verified_fact" and (not r["evidence_id"] or r["source_authority"] != "official_primary"):
            bad["verified_without_evidence"].append(r["fact_id"])
        if r["evidence_id"] and r["ev_src"] != r["source_id"]:
            bad["evidence_source_mismatch"].append(r["fact_id"])
        if r["extractor"].startswith("ai:") and r["epistemic"] not in ("ai_inference", "unverified"):
            bad["ai_as_fact"].append(r["fact_id"])
        if r["value_state"] in ("stated", "explicitly_none") and not r["evidence_id"]:
            bad["stated_without_evidence"].append(r["fact_id"])
        if r["epistemic"] == "verified_fact" and r["fresh_until"] and r["fresh_until"] < now:
            stale_verified += 1
    for code, ids in bad.items():
        for fid in ids:
            c.infra.execute("INSERT INTO failure_queue(failure_id,stage,subject_id,input_digest,error_code,first_seen,last_seen,attempts,resume_from)"
                            " VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(stage,subject_id,input_digest) DO UPDATE SET last_seen=excluded.last_seen",
                            (stable_id("fq", fid, code), "evidence_verification", fid, code, code, now, now, "evidence_verification"))
    epi = dict(c.svc.execute("SELECT epistemic, count(*) FROM pb_fact WHERE superseded_at IS NULL GROUP BY epistemic").fetchall())
    conflicts_open = c.svc.execute("SELECT count(*) FROM pb_conflict_group WHERE resolved_at IS NULL").fetchone()[0]
    return {"live_facts": len(rows), **{"viol_" + k: len(v) for k, v in bad.items()}, "verified_stale_needs_recheck": stale_verified,
            "conflicts_open_kept_for_review": conflicts_open, "by_epistemic": epi}


# 7 ---------------------------------------------------------------- state / history update
def _live(c, call_id, pred):
    return c.svc.execute("SELECT * FROM pb_fact WHERE subject_id=? AND predicate=? AND superseded_at IS NULL ORDER BY recorded_at DESC LIMIT 1",
                         (call_id, pred)).fetchone()


def s_state_update(c: RunContext) -> dict:
    now = c.clock()
    nowdt = timeutil.parse_iso(now)
    moved = {"closed_by_clock": 0, "opened_by_clock": 0, "closed_by_evidence": 0, "cancelled_by_evidence": 0, "reopened_by_evidence": 0}
    for call in c.svc.execute("SELECT call_id, status, status_since, edition_key FROM pb_call_edition WHERE status IN "
                              "('announced','open','extended','standing','closed')").fetchall():
        cid, st = call["call_id"], call["status"]
        decl = _live(c, cid, "status.declared")
        if decl is not None and decl["value_json"] and decl["recorded_at"] > (call["status_since"] or ""):
            d = json.loads(decl["value_json"]).get("declared")
            target = {"cancelled": "cancelled", "closed": "closed"}.get(d)
            if d == "open" and st == "closed":
                target = "open"
            if target and target != st:
                try:
                    statemachine.transition(c.svc, cid, target, "evidence", now, "daemon", "source declares " + d, evidence_id=decl["evidence_id"], fact_id=decl["fact_id"])
                    moved[{"cancelled": "cancelled_by_evidence", "closed": "closed_by_evidence", "open": "reopened_by_evidence"}[target]] += 1
                    continue
                except ValidationError:
                    pass
        closes = _live(c, cid, "deadline.closes_on")
        cv = json.loads(closes["value_json"]) if closes is not None and closes["value_json"] else \
            ({"date": call["edition_key"]} if len(call["edition_key"]) == 10 and call["edition_key"][:2] == "20" else None)
        date = cv.get("date") if cv else None
        if st in ("open", "extended") and date:
            _, latest = project.deadline_bounds(cv)
            if nowdt > latest:                                  # past the latest possible instant in any timezone
                statemachine.transition(c.svc, cid, "closed", "system_clock", now, "daemon", "deadline_passed (%s); pending source confirmation" % date)
                moved["closed_by_clock"] += 1
                continue
        if st == "announced":
            opens = _live(c, cid, "deadline.opens_on")
            od = json.loads(opens["value_json"]).get("date") if opens is not None and opens["value_json"] else None
            if od and od <= now[:10]:
                statemachine.transition(c.svc, cid, "open", "system_clock", now, "daemon", "opens_on reached (%s)" % od)
                moved["opened_by_clock"] += 1
    by = dict(c.svc.execute("SELECT status, count(*) FROM pb_call_edition GROUP BY status").fetchall())
    hist = c.svc.execute("SELECT count(*) FROM pb_status_event").fetchone()[0]
    return {**moved, "calls_by_status": by, "status_events_total": hist}


# 8 ---------------------------------------------------------------- service recompute
def s_service_recompute(c: RunContext) -> dict:
    now = c.clock()
    res = project.recompute(c.svc, now)
    rows = [json.loads(r[0]) for r in c.svc.execute("SELECT payload FROM pb_search_projection ORDER BY call_id")]
    exp = os.path.join(c.root, "exports")
    os.makedirs(exp, exist_ok=True)
    tmp = os.path.join(exp, "projection_latest.json.tmp")
    json.dump({"as_of": now, "calls": rows}, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(exp, "projection_latest.json"))
    res["projection_rows"] = len(rows)
    res["applicable_now"] = {}
    for r in rows:
        res["applicable_now"][r["applicable_now"]] = res["applicable_now"].get(r["applicable_now"], 0) + 1
    return res


# 9 ---------------------------------------------------------------- quality check
def s_quality_check(c: RunContext) -> dict:
    now = c.clock()
    svc = c.svc
    q: Dict[str, object] = {}
    # projection must point only at live facts
    dangling = 0
    for (payload,) in svc.execute("SELECT payload FROM pb_search_projection"):
        p = json.loads(payload)
        for cond in p["conditions"].values():
            for f in cond["facts"]:
                r = svc.execute("SELECT superseded_at FROM pb_fact WHERE fact_id=?", (f["fact_id"],)).fetchone()
                if r is None or r[0] is not None:
                    dangling += 1
    q["projection_points_to_superseded"] = dangling
    q["projection_behind_dirty"] = svc.execute("SELECT count(*) FROM pb_call_edition c LEFT JOIN pb_search_projection p ON p.call_id=c.call_id "
                                               "WHERE c.status NOT IN ('superseded','archived') AND (p.call_id IS NULL OR p.built_for_dirty_seq<c.dirty_seq)").fetchone()[0]
    q["verified_without_evidence"] = svc.execute("SELECT count(*) FROM pb_fact WHERE superseded_at IS NULL AND epistemic='verified_fact' AND evidence_id IS NULL").fetchone()[0]
    q["ai_facts_promoted"] = svc.execute("SELECT count(*) FROM pb_fact WHERE extractor LIKE 'ai:%' AND epistemic NOT IN ('ai_inference','unverified')").fetchone()[0]
    q["manual_clip_above_claim"] = svc.execute("SELECT count(*) FROM pb_fact f JOIN pb_source s ON s.source_id=f.source_id WHERE s.source_type='manual_clip' AND f.epistemic='verified_fact'").fetchone()[0]
    q["stale_live_facts"] = svc.execute("SELECT count(*) FROM pb_fact WHERE superseded_at IS NULL AND fresh_until IS NOT NULL AND fresh_until<?", (now,)).fetchone()[0]
    q["conflicts_open"] = svc.execute("SELECT count(*) FROM pb_conflict_group WHERE resolved_at IS NULL").fetchone()[0]
    q["dead_letter_jobs"] = c.infra.execute("SELECT count(*) FROM job WHERE state='dead'").fetchone()[0]
    q["failure_queue_open"] = c.infra.execute("SELECT count(*) FROM failure_queue WHERE state='open'").fetchone()[0]
    # Raw history is authoritative: historical successful writers left last_success_at NULL.
    q["sources_never_succeeded"] = svc.execute("SELECT count(*) FROM pb_source s WHERE s.permission IN ('allowed_public','allowed_conditional') "
                                              "AND NOT EXISTS (SELECT 1 FROM pb_raw_ref r WHERE r.source_id=s.source_id AND r.outcome='ok')").fetchone()[0]
    fetch = c.state.get("incremental_fetch", {})
    chg = c.state.get("change_detection", {})
    prev_empty = c.infra.execute("SELECT count(*) FROM (SELECT metrics FROM metric_snapshot WHERE label='daemon_quality' ORDER BY taken_at DESC LIMIT 2) "
                                 "WHERE json_extract(metrics,'$.empty_run')=1").fetchone()[0]
    q["empty_run"] = 1 if (not c.opts.get("offline") and fetch.get("pages_ok", 0) == 0 and chg.get("new_or_changed_revisions", 0) == 0) else 0
    q["empty_run_streak_alert"] = 1 if q["empty_run"] and prev_empty >= 2 else 0
    open_rows = svc.execute("SELECT call_id, payload FROM pb_search_projection").fetchall()
    q["open_calls_applicable_yes_or_maybe"] = sum(1 for r in open_rows if json.loads(r["payload"])["applicable_now"] in ("yes", "maybe"))
    must_be_zero = ["projection_points_to_superseded", "verified_without_evidence", "ai_facts_promoted", "manual_clip_above_claim", "projection_behind_dirty"]
    q["violations"] = [k for k in must_be_zero if q[k]]
    c.infra.execute("INSERT INTO metric_snapshot(snapshot_id,taken_at,label,metrics) VALUES(?,?,?,?)",
                    ("ms_%s_%s" % (c.run_id, now), now, "daemon_quality", json.dumps(q, ensure_ascii=False)))
    if q["violations"] or q["empty_run_streak_alert"]:
        store.alert(c.infra, "daemon_quality", "daemon://" + c.run_id, q, None, now)
    return q


STAGES = [("discovery", s_discovery), ("incremental_fetch", s_incremental_fetch), ("change_detection", s_change_detection),
          ("extraction", s_extraction), ("entity_resolution", s_entity_resolution), ("evidence_verification", s_evidence_verification),
          ("state_update", s_state_update), ("service_recompute", s_service_recompute), ("quality_check", s_quality_check)]
