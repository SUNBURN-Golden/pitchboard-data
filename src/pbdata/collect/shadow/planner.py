"""Metadata-only gap planning. Never imports the service DB writer or collector."""
from collections import Counter, defaultdict, deque
import datetime as dt
import os
from pathlib import Path
import re
import sqlite3

from .common import Stop, digest, dumps, loads, no_symlinks, stamp
from .policy import canonical_url

EXPORT_SCHEMA = "pb-shadow-gaps-1"
PRIORITY = {p: i for i, p in enumerate(("fee.submission", "deadline.closes_on", "status.declared",
            "compensation.rate", "eligibility.region", "eligibility.statement"))}
STATIC = {"fee.submission", "compensation.rate", "eligibility.region", "eligibility.statement"}


def read_json(path, now, *, allow_stale=False):
    path = no_symlinks(path)
    if path.stat().st_size > 8_000_000:
        raise Stop("gap_input_too_large")
    try:
        data = loads(path.read_bytes())
        if not isinstance(data, dict) or set(data) != {"schema", "exported_at_utc", "rows"} or data["schema"] != EXPORT_SCHEMA:
            raise Stop("invalid_gap_export")
        age = now - stamp(data["exported_at_utc"])
        if age < dt.timedelta(0) or (age > dt.timedelta(hours=24) and not allow_stale):
            raise Stop("stale_gap_export")
        if not isinstance(data["rows"], list) or len(data["rows"]) > 20000:
            raise Stop("invalid_gap_export")
        return data
    except (ValueError, TypeError, KeyError):
        raise Stop("invalid_gap_export") from None


def read_db(path, now):
    """Read a stable rollback-journal DB into memory with O_RDONLY only.

    SQLite never opens the input file: it cannot create sidecars. Active journals
    and WAL format require a consistent operator export. No immutable live reads.
    """
    path = no_symlinks(path)
    def snapshot():
        s = path.stat()
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise Stop("db_requires_export")
        return s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
    before = snapshot()
    if before[1] > 256_000_000:
        raise Stop("gap_input_too_large")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        raw = f.read(256_000_001)
    if snapshot() != before or raw[:16] != b"SQLite format 3\x00" or raw[18:20] != b"\x01\x01":
        raise Stop("db_requires_export")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.deserialize(raw)
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute('''
        SELECT g.gap_id,g.subject_type,g.subject_id,g.predicate,g.scope_ref,g.state,g.gap_class,g.importance,
          s.owner_outlet_id,s.source_id,s.owner_name,o.canonical_name AS outlet_name,s.permission,
          r.raw_id,r.url,r.final_url,r.kind,r.outcome,r.http_status,r.content_hash,r.text_hash,r.fetched_at,
          CASE WHEN EXISTS(SELECT 1 FROM pb_extraction e WHERE e.raw_id=r.raw_id AND e.field='signal.cue'
            AND json_extract(e.value_json,'$.for')=g.predicate) THEN 0
            WHEN EXISTS(SELECT 1 FROM pb_label l WHERE l.raw_id=r.raw_id AND l.label IN ('rules','recruitment')
              AND l.status NOT IN ('stale_prior_year','results_not_call','past_deadline','not_writing_call')) THEN 1 ELSE 2 END AS relevance,
          'outlet_source' AS relationship_basis
        FROM pb_gap g LEFT JOIN pb_call_edition c ON g.subject_type='call' AND c.call_id=g.subject_id
        LEFT JOIN pb_source s ON s.owner_outlet_id=CASE WHEN g.subject_type='outlet' THEN g.subject_id ELSE c.outlet_id END
        LEFT JOIN pb_outlet o ON o.outlet_id=s.owner_outlet_id
        LEFT JOIN pb_raw_ref r ON r.source_id=s.source_id
        WHERE g.state='open' AND g.gap_class='not_extracted'
        ''').fetchall()
        # Include latest observations from every source, including unrelated denied
        # observations of the same URL, so an older success cannot win the join.
        latest = {r["url"]: r["raw_id"] for r in conn.execute(
            "SELECT url,raw_id FROM pb_raw_ref ORDER BY fetched_at,raw_id")}
        result = [dict(r) for r in rows if r["url"] is None or latest[r["url"]] == r["raw_id"]]
        return {"schema": EXPORT_SCHEMA, "exported_at_utc": now.isoformat(), "rows": result}
    except (sqlite3.Error, ValueError):
        raise Stop("invalid_gap_database") from None
    finally:
        conn.close()


def plan(data, *, limit=40, history=(), config_hash=None):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise Stop("invalid_limit")
    from .schema import PROMPT_HASH, SCHEMA_HASH
    reasons, candidates = Counter(), []
    rows = data["rows"]
    latest = {}
    for row in rows:
        if not isinstance(row, dict):
            raise Stop("invalid_gap_export")
        if row.get("url"):
            try:
                rank = (stamp(row["fetched_at"]), row["raw_id"])
                if not isinstance(rank[1], str):
                    raise ValueError()
                latest[row["url"]] = max(latest.get(row["url"], rank), rank)
            except (KeyError, TypeError, ValueError):
                raise Stop("invalid_gap_export") from None
    for original in rows:
        r = dict(original)
        try:
            for key in ("gap_id", "subject_type", "subject_id", "predicate", "state", "gap_class", "scope_ref"):
                if not isinstance(r.get(key), str) or not 0 < len(r[key]) <= 280:
                    raise Stop("invalid_gap_export")
            if r["state"] != "open" or r["gap_class"] != "not_extracted":
                raise Stop("not_open_target")
            r["predicate"] = {"submission_fee": "fee.submission"}.get(r["predicate"], r["predicate"])
            if r["predicate"] not in PRIORITY:
                raise Stop("unsupported_predicate")
            if r["subject_type"] not in ("call", "outlet"):
                raise Stop("unsupported_subject")
            if not r.get("owner_outlet_id"):
                raise Stop("no_outlet_relation")
            if any("soulbound" in str(r.get(k, "")).casefold() for k in
                   ("owner_outlet_id", "owner_name", "outlet_name", "source_id", "url", "final_url")):
                raise Stop("excluded_owner")
            if not r.get("url"):
                raise Stop("no_related_url")
            if (stamp(r["fetched_at"]), r["raw_id"]) != latest[r["url"]]:
                raise Stop("superseded_observation")
            if r.get("kind") != "html" or r.get("outcome") != "ok" or r.get("permission") not in ("allowed_public", "allowed_conditional"):
                raise Stop("ineligible_source")
            status = r.get("http_status")
            if status is not None and (type(status) is not int or not 200 <= status < 300):
                raise Stop("ineligible_source")
            r["url"] = canonical_url(r["url"])
            r["final_url"] = canonical_url(r.get("final_url") or r["url"])
            for key in ("content_hash", "text_hash"):
                if r.get(key) is not None and not re.fullmatch(r"[a-f0-9]{64}", r[key]):
                    raise Stop("invalid_gap_export")
            if not isinstance(r.get("source_id"), str) or r.get("relationship_basis") != "outlet_source":
                raise Stop("invalid_gap_export")
            importance = r.get("importance", .5)
            if type(importance) not in (int, float) or not 0 <= importance <= 1:
                raise Stop("invalid_gap_export")
            done = any(h.get("outcome") == "ok" and h.get("final_url") == r["final_url"] and r.get("content_hash")
                       and h.get("body_sha256") == r["content_hash"] and h.get("prompt_sha256") == PROMPT_HASH
                       and h.get("schema_sha256") == SCHEMA_HASH and h.get("config_sha256") == config_hash for h in history)
            if done and r["predicate"] in STATIC:
                raise Stop("skip_static_already_done")
            if done:
                # Explicit next-day temporal recheck; same-day dedup still applies.
                reasons["retry_time_sensitive"] += 1
            candidates.append(r)
        except Stop as exc:
            if str(exc) == "invalid_gap_export":
                raise
            reasons[str(exc)] += 1
    candidates.sort(key=lambda r: (PRIORITY[r["predicate"]], -r.get("importance", .5),
                                  r.get("relevance", 2), r["gap_id"], r["url"]))
    ordered = []
    for tier in sorted(set(PRIORITY[r["predicate"]] for r in candidates)):
        outlets = defaultdict(deque)
        for r in candidates:
            if PRIORITY[r["predicate"]] == tier:
                outlets[r["owner_outlet_id"]].append(r)
        while any(outlets.values()):
            for outlet in sorted(outlets):
                if outlets[outlet]:
                    ordered.append(outlets[outlet].popleft())
    targets = {}
    for r in ordered:
        if r["url"] not in targets:
            targets[r["url"]] = {"url": r["url"], "content_hash": r.get("content_hash"), "targets": []}
        targets[r["url"]]["targets"].append(r)
    return {"schema": "pb-shadow-plan-1", "exported_at_utc": data["exported_at_utc"],
            "input_sha256": digest(dumps(data)), "targets": list(targets.values())[:limit], "reasons": dict(reasons)}
