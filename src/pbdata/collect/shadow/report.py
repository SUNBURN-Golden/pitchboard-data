"""Offline targeted-run scoring. Agreement is never used as human gold."""
from collections import Counter, defaultdict
import datetime as dt
import os
from pathlib import Path
import re
import sqlite3

from .common import Stop, digest, dumps, loads, no_symlinks, secure_open, sqlite_snapshot, stamp, total_cost
from .compare import boolean_metrics
from .schema import VERSION, FIELDS, PROMPT_HASH, SCHEMA_HASH, validator
from . import source_cache
from .review import reviews, monetary
from .policy import canonical_url


def frozen_read(path, checks, maximum=32_000_000, private=False):
    path = no_symlinks(path)
    before = path.stat()
    if before.st_size > maximum:
        raise Stop("report_input_too_large")
    if private:
        with secure_open(path, "r", create=False) as f:
            data = f.read(maximum + 1).encode("utf-8")
    else:
        data = path.read_bytes()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise Stop("report_input_changed")
    entry = {"sha256": digest(data), "size": after.st_size, "mtime_ns": after.st_mtime_ns}
    if str(path) in checks and checks[str(path)] != entry:
        raise Stop("report_input_changed")
    checks[str(path)] = entry
    return data


def identity(row):
    try:
        values = tuple(row[k] for k in ("requested_url", "final_url", "body_sha256", "text_sha256", "as_of", "schema_sha256", "prompt_sha256"))
        if not all(isinstance(v, str) for v in values):
            raise ValueError()
        if any(not re.fullmatch(r"[a-f0-9]{64}", values[i]) for i in (2, 3, 5, 6)):
            raise ValueError()
        if dt.date.fromisoformat(values[4]).isoformat() != values[4]:
            raise ValueError()
        return (canonical_url(values[0]), canonical_url(values[1]), *values[2:])
    except (KeyError, TypeError, ValueError):
        raise Stop("invalid_prediction") from None


def load_rows(paths, checks):
    rows = {}
    for path in paths:
        data = frozen_read(path, checks)
        if data and not data.endswith(b"\n"):
            raise Stop("partial_shadow_output")
        for line in data.splitlines():
            try:
                r = loads(line)
                if not isinstance(r, dict) or not isinstance(r.get("attempt_id"), str) or not isinstance(r.get("outcome"), str):
                    raise Stop("invalid_prediction")
                if r["attempt_id"] in rows and rows[r["attempt_id"]] != r:
                    raise Stop("conflicting_predictions")
                rows[r["attempt_id"]] = r
            except (ValueError, TypeError):
                raise Stop("invalid_prediction") from None
    return list(rows.values())


def load_gold(path, checks):
    if path is None:
        return {}
    try:
        data = loads(frozen_read(path, checks, 8_000_000))
        if not isinstance(data, dict) or set(data) != {"schema", "rows"} or data["schema"] != "pb-shadow-gold-1" or not isinstance(data["rows"], list):
            raise Stop("invalid_gold")
        result = {}
        for row in data["rows"]:
            key = identity(row)
            if row.get("scope_ref") != "page" or not isinstance(row.get("reviewer"), str) or not row["reviewer"]:
                raise Stop("invalid_gold")
            stamp(row["reviewed_at_utc"])
            for name in ("real_call", "current_open"):
                if row.get(name) is not None and type(row[name]) is not bool:
                    raise Stop("invalid_gold")
            evidence = row.get("evidence", {})
            if not isinstance(evidence, dict) or set(evidence) - (set(FIELDS[1:]) | {"real_call", "current_open"}):
                raise Stop("invalid_gold")
            required_evidence = {name for name in ("real_call", "current_open") if row.get(name) is not None}
            fs = row.get("fields", {})
            if not isinstance(fs, dict) or set(fs) - set(FIELDS[1:]):
                raise Stop("invalid_gold")
            from .schema import valid_value
            for f, value in fs.items():
                if value is not None and not valid_value(f, value):
                    raise Stop("invalid_gold")
            required_evidence.update(f for f, value in fs.items() if value is not None)
            if any(not isinstance(evidence.get(f), str) or not 1 <= len(evidence[f]) <= 280 for f in required_evidence):
                raise Stop("invalid_gold")
            if key in result and result[key] != row:
                raise Stop("conflicting_gold")
            result[key] = row
        return result
    except (KeyError, TypeError, ValueError):
        raise Stop("invalid_gold") from None


def ledger_report(path, rows, checks):
    if path is None or not Path(path).exists():
        return {"complete": False, "reason": "missing_ledger", "ceiling_usd": None}
    path = no_symlinks(path)
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise Stop("ledger_requires_snapshot")
    # SQLite only sees the immutable byte snapshot.
    raw = frozen_read(path, checks)
    if raw[:16] != b"SQLite format 3\x00" or raw[18:20] != b"\x01\x01":
        raise Stop("invalid_ledger")
    try:
        with sqlite_snapshot(raw) as c:
            if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise Stop("invalid_ledger")
            has_calls = any(row["name"] == "provider_calls" for row in c.execute("PRAGMA table_info(attempts)"))
            attempts = list(c.execute("SELECT id,day,reserved_usd,result,%s FROM attempts" %
                                      ("provider_calls" if has_calls else "NULL AS provider_calls")))
            meta = dict(c.execute("SELECT key,value FROM meta"))
    except sqlite3.Error:
        raise Stop("invalid_ledger") from None
    days, discrepancies, snapshots = {}, [], {}
    input_ids = {r["attempt_id"] for r in rows}
    attempt_ids = {r["id"] for r in attempts}
    by_id = {r["attempt_id"]: r for r in rows}
    groups = defaultdict(list)
    ceilings = []
    for a in attempts:
        ceiling = None if a["reserved_usd"] == "unknown" else a["reserved_usd"]
        ceilings.append(ceiling)
        r = None
        if a["result"] is not None:
            r = loads(a["result"])
            if r.get("experiment_id") != meta.get("experiment_id") or r.get("attempt_id") != a["id"] or r.get("budget_day") != a["day"]:
                discrepancies.append({"attempt_id": a["id"], "reason": "ledger_identity_mismatch"})
            if r.get("charged_ceiling_usd") != ceiling:
                discrepancies.append({"attempt_id": a["id"], "reason": "ledger_charge_mismatch"})
            if a["provider_calls"] is not None and r.get("provider_calls") != a["provider_calls"]:
                discrepancies.append({"attempt_id": a["id"], "reason": "ledger_call_count_mismatch"})
            if a["id"] in by_id and r != by_id[a["id"]]:
                discrepancies.append({"attempt_id": a["id"], "reason": "ledger_result_mismatch"})
            snapshot = r.get("config_snapshot")
            if snapshot:
                if digest(dumps(snapshot)) != r.get("config_sha256"):
                    raise Stop("config_snapshot_mismatch")
                snapshots[r["config_sha256"]] = snapshot
        calls = a["provider_calls"]
        if calls is None and r is not None:
            calls = r.get("provider_calls", 1)
        groups[a["day"]].append((r, calls, ceiling))
    for day, entries in sorted(groups.items()):
        completed = [row for row, _, _ in entries if row is not None]
        daily_configs = {r.get("config_sha256") for r in completed}
        unfinished = len(entries) - len(completed)
        call_counts = [calls for _, calls, _ in entries]
        days[day] = {"attempts": len(entries), "unfinished": unfinished,
                     "successful": sum(r["outcome"] == "ok" for r in completed),
                     "failed": sum(r["outcome"] != "ok" for r in completed),
                     "unknown_usage": sum(not r.get("usage") for r in completed) + unfinished,
                     "provider_calls": None if None in call_counts else sum(call_counts),
                     "reserved_ceiling_usd": total_cost(ceiling for _, _, ceiling in entries),
                     "estimated_cost_usd": total_cost(r.get("estimated_cost_usd") if r else None for r, _, _ in entries),
                     "config_hashes": sorted(h for h in daily_configs if h),
                     "outcomes": dict(Counter(r["outcome"] for r in completed))}
    for missing in sorted(attempt_ids - input_ids):
        discrepancies.append({"attempt_id": missing, "reason": "ledger_attempt_missing_output"})
    for extra in sorted(input_ids - attempt_ids):
        discrepancies.append({"attempt_id": extra, "reason": "output_missing_ledger_attempt"})
    return {"complete": not discrepancies, "experiment_id": meta.get("experiment_id"), "halt_reason": meta.get("halted"),
            "days": days, "ceiling_usd": total_cost(ceilings),
            "pricing_config_snapshots": snapshots, "discrepancies": discrepancies,
            "meaning": "Logged cost estimates and optional dispatch ceilings; null means unavailable."}


def signature(field, value):
    if field == "deadline":
        return value.get("date")
    if field in ("pay", "submission_fee"):
        state = value.get("state")
        return dumps([state, monetary(value)])
    if field == "simultaneous":
        return value.get("state")
    return value.get("text")


def rule_signatures(field, rules):
    predicate = {"deadline": "deadline.closes_on", "pay": "compensation.rate", "submission_fee": "fee.submission", "eligibility": "eligibility.statement", "simultaneous": "rights.simultaneous"}[field]
    result = set()
    for e in rules["extensions"]:
        if e["field"] != predicate:
            continue
        v = dict(e["value"])
        if field in ("pay", "submission_fee"):
            v["state"] = (("unpaid" if field == "pay" else "explicitly_none") if e["value_state"] == "explicitly_none" else ("paid" if field == "pay" else "stated"))
        sig = signature(field, v)
        if sig is not None:
            result.add(sig)
    return result


def field_metrics(pairs):
    tp = sum(len(g & p) for g, p in pairs)
    fp = sum(len(p - g) for g, p in pairs)
    fn = sum(len(g - p) for g, p in pairs)
    return {"labeled_pages": len(pairs), "gold_values": sum(len(g) for g, _ in pairs),
            "predicted_values": sum(len(p) for _, p in pairs), "tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "accuracy_end_to_end": sum(g == p for g, p in pairs) / len(pairs) if pairs else None,
            "reason": None if pairs else "missing_gold"}


def plans_report(root, rows, checks):
    days = {r.get("budget_day") for r in rows if r.get("budget_day")}
    selected, observed, reasons, ids = set(), set(), Counter(), set()
    found = False
    for day in sorted(days):
        path = Path(root) / "data/shadow" / ("plans_" + day.replace("-", "") + ".jsonl")
        if not path.exists() and not path.is_symlink():
            continue
        found = True
        data = frozen_read(path, checks)
        if data and not data.endswith(b"\n"):
            raise Stop("partial_shadow_output")
        for line in data.splitlines():
            value = loads(line)
            if not isinstance(value, dict) or value.get("plan_id") != digest(dumps(value.get("plan"))):
                raise Stop("invalid_plan_history")
            if value["plan_id"] in ids:
                continue
            ids.add(value["plan_id"])
            plan = value["plan"]
            if plan.get("schema") != "pb-shadow-plan-1" or not isinstance(plan.get("targets"), list):
                raise Stop("invalid_plan_history")
            for target in plan["targets"]:
                selected.add((day, canonical_url(target["url"])))
            reasons.update(plan["reasons"])
    for row in rows:
        if row.get("budget_day") and row.get("requested_url"):
            observed.add((row["budget_day"], canonical_url(row["requested_url"])))
    return {"selected_urls": len(selected) if found else None,
            "selected_without_attempt": len(selected - observed) if found else None,
            "exclusion_reasons": dict(reasons), "reason": None if found else "missing_plan_history"}


def score_shadow(paths, root, now, config, *, gold_path=None, ledger_path=None, max_page_utf8_bytes=24000):
    if not isinstance(config, dict):
        raise Stop("invalid_config")
    checks = {}
    rows = load_rows(paths, checks)
    gold = load_gold(gold_path, checks)
    ledger = ledger_report(ledger_path, rows, checks)
    plans = plans_report(root, rows, checks)
    exclusions, verified, repeats, seen = [], [], [], {}
    for row in rows:
        if row["outcome"] != "ok":
            exclusions.append({"attempt_id": row["attempt_id"], "reason": "unsuccessful_attempt"})
            continue
        key = identity(row)
        if row.get("schema") != VERSION:
            exclusions.append({"attempt_id": row["attempt_id"], "reason": "legacy_or_unsupported_schema"})
            continue
        if row.get("schema_sha256") != SCHEMA_HASH or row.get("prompt_sha256") != PROMPT_HASH:
            exclusions.append({"attempt_id": row["attempt_id"], "reason": "schema_prompt_mismatch"})
            continue
        # New rows record the service contract hash; legacy rows fall back to the
        # raw byte identity (their old cache format is no longer reused).
        source_hash = row.get("source_content_hash") or row["body_sha256"]
        if not isinstance(source_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", source_hash):
            exclusions.append({"attempt_id": row["attempt_id"], "reason": "invalid_source_identity"})
            continue
        cache_paths = source_cache.candidate_paths(root, row["requested_url"], source_hash)
        has_cache = bool(cache_paths)
        for cache_path in cache_paths:
            frozen_read(cache_path, checks, source_cache.MAX_ENVELOPE, private=True)
        try:
            cached = (source_cache.load(root, row["requested_url"], source_hash, now, max_page_utf8_bytes,
                                        final_url=row["final_url"], text_sha256=row["text_sha256"])
                      if has_cache else None)
        except Stop as exc:
            if str(exc) != "page_too_large":
                raise
            exclusions.append({"attempt_id": row["attempt_id"], "reason": "unverifiable_page_too_large"})
            continue
        if cached is None:
            exclusions.append({"attempt_id": row["attempt_id"], "reason": "unverifiable_missing_or_expired_text"})
            continue
        for cache_path in cache_paths:
            frozen_read(cache_path, checks, source_cache.MAX_ENVELOPE, private=True)
        if (cached.cache_key != row.get("source_cache_key") or digest(cached.doc().text()) != row["text_sha256"]
                or cached.final_url != row["final_url"] or cached.body_sha256 != row["body_sha256"]):
            exclusions.append({"attempt_id": row["attempt_id"], "reason": "source_identity_mismatch"})
            continue
        try:
            raw = {f: ({"value": row["fields"][f]["value"], "quote": row["fields"][f]["quote"]} if row["fields"][f] else None) for f in FIELDS}
            fields, reasons = validator(row["schema"])(raw, cached.doc(), row["as_of"])
            if reasons or fields != row["fields"]:
                exclusions.append({"attempt_id": row["attempt_id"], "reason": "invalid_evidence"})
                continue
            rules = row["baseline"]
            if (not isinstance(rules, dict) or type(rules.get("real_call")) is not bool or type(rules.get("current_open")) is not bool
                    or not isinstance(rules.get("extensions"), list) or not isinstance(rules.get("labels"), list) or not isinstance(rules.get("deadline"), list)):
                raise Stop("invalid_baseline")
            rs = reviews(row)
        except (KeyError, TypeError, ValueError):
            raise Stop("invalid_prediction_fields") from None
        if key in seen:
            if row["fields"] != seen[key]["fields"] or row["baseline"] != seen[key]["baseline"]:
                raise Stop("conflicting_predictions")
            repeats.append(row["attempt_id"])
            continue
        seen[key] = row
        verified.append({"key": key, "row": row, "fields": fields, "reviews": rs, "gold": gold.get(key),
                         "doc": cached.doc(), "language": "ko" if cached.doc().lang.startswith("ko") or re.search("[가-힣]", cached.doc().text()) else "en_or_other"})
    boolean = {name: {model: [] for model in ("rules", "llm")} for name in ("real_call", "current_open")}
    extracted = {field: {model: [] for model in ("rules", "llm")} for field in FIELDS[1:]}
    coverage = {field: {"accepted": 0, "verified_pages": len(verified), "targeted_pages": 0, "accepted_targeted": 0} for field in FIELDS}
    predicates = {"open_call": {"status.declared"}, "deadline": {"deadline.closes_on"}, "pay": {"compensation.rate"},
                  "submission_fee": {"fee.submission"}, "simultaneous": {"rights.simultaneous"}, "eligibility": {"eligibility.region", "eligibility.statement"}}
    agreement, conflict_count, second_count, fills = [], 0, 0, Counter()
    strata = Counter()
    stratified_coverage = defaultdict(Counter)
    for item in verified:
        row, fields, g = item["row"], item["fields"], item["gold"]
        call = fields["open_call"]
        real = call["value"]["is_real_call"] if call else None
        current = call["value"]["status"] == "open" if call else None
        if current is not None:
            agreement.append(current == row["baseline"]["current_open"])
        targets = (row.get("gap_target") or {}).get("targets", [])
        target_preds = {t["predicate"] for t in targets}
        strata[(item["language"], "gap_targeted" if targets else "other_shadow")] += 1
        for f in FIELDS:
            stratified_coverage[(item["language"], "gap_targeted" if targets else "other_shadow")][f] += fields[f] is not None
            coverage[f]["accepted"] += fields[f] is not None
            targeted = bool(target_preds & predicates[f])
            coverage[f]["targeted_pages"] += targeted
            coverage[f]["accepted_targeted"] += targeted and fields[f] is not None and not row.get("source_changed")
        for r in item["reviews"]:
            conflict_count += bool(set(r["reasons"]) & {"field_conflict", "call_disagreement", "ambiguous_rules"})
            second_count += r["second_opinion_required"]
            if "candidate_fill" in r["reasons"] and not row.get("source_changed"):
                fills[r["predicate"]] += 1
        if g:
            if any(not any(quote in segment.text for segment in item["doc"].segments) for quote in g.get("evidence", {}).values()):
                raise Stop("invalid_gold_evidence")
            for field, expected in g.get("fields", {}).items():
                if expected is not None:
                    raw_gold = dict.fromkeys(FIELDS)
                    raw_gold[field] = {"value": expected, "quote": g["evidence"][field]}
                    accepted, invalid = validator(VERSION)(raw_gold, item["doc"], row["as_of"])
                    if invalid or accepted[field] is None:
                        raise Stop("invalid_gold_evidence")
            for name, prediction in (("real_call", real), ("current_open", current)):
                if g.get(name) is not None:
                    boolean[name]["rules"].append((g[name], row["baseline"][name]))
                    boolean[name]["llm"].append((g[name], prediction))
            for field, value in g.get("fields", {}).items():
                expected = {signature(field, value)} if value is not None else set()
                predicted = {signature(field, fields[field]["value"])} if fields[field] else set()
                predicted.discard(None); expected.discard(None)
                extracted[field]["llm"].append((expected, predicted))
                extracted[field]["rules"].append((expected, rule_signatures(field, row["baseline"])))
    for f, counts in coverage.items():
        counts["coverage"] = counts["accepted"] / len(verified) if verified else None
        counts["targeted_coverage"] = counts["accepted_targeted"] / counts["targeted_pages"] if counts["targeted_pages"] else None
    for path in list(checks):
        frozen_read(path, checks)
    return {"schema": "pb-shadow-comparison-1", "scored_at_utc": now.isoformat(),
            "accuracy": {name: {model: boolean_metrics(pairs) for model, pairs in models.items()} for name, models in boolean.items()},
            "field_accuracy": {f: {m: field_metrics(pairs) for m, pairs in ms.items()} for f, ms in extracted.items()},
            "field_coverage": coverage, "agreement": {"current_open": {"paired": len(agreement), "agreed": sum(agreement),
              "rate": sum(agreement) / len(agreement) if agreement else None}},
            "counts": {"attempted": len(rows), "successful_attempts": sum(r["outcome"] == "ok" for r in rows),
                       "verified_unique_pages": len(verified), "repeat_attempts": len(repeats),
                       "changed_source_attempts": sum(bool(r.get("source_changed")) for r in rows), "conflict_fields": conflict_count, "second_opinions": second_count, "labeled_pages": sum(i["gold"] is not None for i in verified)},
            "planning": plans, "candidate_fills": dict(fills), "exclusions": exclusions, "repeat_attempt_ids": repeats,
            "unmatched_gold": sum(k not in seen for k in gold), "ledger": ledger,
            "strata": [{"language": lang, "tier": tier, "pages": n, "field_coverage": dict(stratified_coverage[(lang, tier)])} for (lang, tier), n in sorted(strata.items())],
            "cohort": [{"attempt_id": i["row"]["attempt_id"], "identity": i["key"], "language": i["language"]} for i in verified],
            "runtime_commits": sorted({r.get("runtime_commit", "unknown") for r in rows}),
            "baseline_versions": sorted({(i["row"]["baseline"].get("extractor_version", "unknown"), i["row"]["baseline"].get("labeler_version", "unknown")) for i in verified}),
            "input_checksums": checks,
            "notes": ["Agreement is not accuracy. Missing human gold leaves accuracy null (missing_gold).",
                      "Gap-targeted cohorts are biased toward weak fields; do not infer population accuracy.",
                      "Human labels must cover negatives and agreements as well as conflicts; small samples remain low-power.",
                      "No refetch, credentials, provider dispatch or service DB is used by this report.",
                      "Ledger dispatch ceilings may include failed and unfinished attempts."]}


def markdown(report):
    c = report["counts"]
    def display(value):
        return "unknown" if value is None else str(value)
    lines = ["# Shadow comparison", "",
             f"Verified pages: {c['verified_unique_pages']}; attempted: {c['attempted']}; labeled: {c['labeled_pages']}.", "",
             f"Conflict fields: {c['conflict_fields']}; second opinions: {c['second_opinions']}.", "",
             "| Gold task | Method | Labeled | Covered | End-to-end accuracy | F1 |",
             "| --- | --- | ---: | ---: | ---: | ---: |"]
    for task, methods in report["accuracy"].items():
        for method, m in methods.items():
            lines.append(f"| {task} | {method} | {m['labeled']} | {m['covered']} | {display(m['accuracy_end_to_end'])} | {display(m['f1'])} |")
    lines += ["", "| Field | Accepted / verified | Accepted / targeted |", "| --- | ---: | ---: |"]
    for field, m in report["field_coverage"].items():
        lines.append(f"| {field} | {m['accepted']} / {m['verified_pages']} | {m['accepted_targeted']} / {m['targeted_pages']} |")
    lines += ["", f"Ledger ceiling: USD {display(report['ledger']['ceiling_usd'])}; reconciliation complete: {report['ledger']['complete']}.", "",
              "Permanent reservation ceilings include failed and unfinished attempts. They are not invoices or actual spend.", "",
              "Agreement is not accuracy. Missing gold leaves accuracy unknown. Targeted cohorts do not establish population accuracy.", "",
              "See JSON for frozen membership/checksums, exclusions, field gold precision/recall, pricing snapshots and discrepancies."]
    return "\n".join(lines) + "\n"
