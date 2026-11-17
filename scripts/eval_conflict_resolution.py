#!/usr/bin/env python3
"""Evaluate conflict repair on a temporary service SQLite copy; never open the original with SQLite.

Usage: python scripts/eval_conflict_resolution.py SERVICE_SQLITE [--now UTC_ISO]
Copy the database and its WAL (if present) before running the resolver.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pbdata import facts, timeutil


def fact_snapshot(conn: sqlite3.Connection) -> dict:
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(pb_fact)") if r["name"] != "conflict_group_id"]
    immutable = hashlib.sha256()
    values = hashlib.sha256()
    count = 0
    for row in conn.execute("SELECT * FROM pb_fact ORDER BY fact_id"):
        count += 1
        immutable.update((json.dumps([row[c] for c in columns], ensure_ascii=False) + "\n").encode("utf-8"))
        values.update((json.dumps([row[c] for c in ("fact_id", "value_json", "epistemic", "superseded_at")],
                                  ensure_ascii=False) + "\n").encode("utf-8"))
    return {"count": count, "non_linkage_sha256": immutable.hexdigest(),
            "value_json_epistemic_superseded_at_sha256": values.hexdigest()}


def open_groups(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in conn.execute("SELECT group_id,subject_type,subject_id,predicate,scope_ref,kind "
                                         "FROM pb_conflict_group WHERE resolved_at IS NULL ORDER BY group_id")]


def evaluate(conn: sqlite3.Connection, now: str) -> dict:
    before_groups = open_groups(conn)
    before_facts = fact_snapshot(conn)
    resolved = set(facts.resolve_noncontradicting_conflicts(conn, now))
    after_facts = fact_snapshot(conn)
    remaining = open_groups(conn)
    return {"conflicts_open": {"before": len(before_groups), "after": len(remaining)},
            "resolved_groups": [g for g in before_groups if g["group_id"] in resolved],
            "remaining_groups": remaining,
            "fact_integrity": {"before": before_facts, "after": after_facts,
                               "unchanged_except_conflict_linkage": before_facts == after_facts}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service_sqlite", type=Path)
    parser.add_argument("--now", default=timeutil.iso(timeutil.utcnow()), help="UTC ISO time for resolution decisions")
    args = parser.parse_args()
    try:
        timeutil.parse_iso(args.now)
    except ValueError as exc:
        parser.error(str(exc))
    original = args.service_sqlite.resolve()
    with tempfile.TemporaryDirectory(prefix="pb-conflict-eval-") as tmp:
        copied = Path(tmp) / "service.sqlite3"
        shutil.copyfile(original, copied)
        wal = Path(str(original) + "-wal")
        if wal.exists():
            shutil.copyfile(wal, str(copied) + "-wal")
        with contextlib.closing(sqlite3.connect(copied, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            report = evaluate(conn, args.now)
            print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["fact_integrity"]["unchanged_except_conflict_linkage"] else 1


if __name__ == "__main__":
    sys.exit(main())
