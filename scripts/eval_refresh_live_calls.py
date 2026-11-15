#!/usr/bin/env python3
"""Compare refresh coverage using only a temporary, read-only service DB copy.

Usage: python scripts/eval_refresh_live_calls.py SERVICE_SQLITE [--now UTC_ISO]
The original DB is never opened by SQLite. Its WAL, if present, is copied too.
"""
from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pbdata import timeutil
from pbdata.collect.collector import refresh_candidates


def evaluate(svc: sqlite3.Connection, now: str) -> dict:
    # Keep the old selection inline and independent of the production helper.
    old = svc.execute(
        "SELECT r.url, r.source_id, r.depth, max(r.fetched_at) AS last FROM pb_raw_ref r "
        "WHERE r.outcome='ok' AND r.kind IN ('html','pdf','image') "
        "GROUP BY r.url ORDER BY last, r.url LIMIT 40").fetchall()
    candidates = refresh_candidates(svc, now, horizon_hours=48)
    needing = [r["url"] for r in candidates if r["refresh_fresh_until"] is not None]
    needed = set(needing)
    # Selection only: skip owner/site lookup consistently for both versions.
    return {
        "now": now,
        "horizon_hours": 48,
        "limit": 40,
        "live_call_urls_needing_refresh": needing,
        "old_selection_live_call_urls_included": sum(r["url"] in needed for r in old),
        "new_selection_live_call_urls_included": sum(r["url"] in needed for r in candidates[:40]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service_sqlite", type=Path)
    parser.add_argument("--now", help="UTC ISO time (default: max pb_raw_ref.fetched_at)")
    args = parser.parse_args()
    original = args.service_sqlite.resolve()
    with tempfile.TemporaryDirectory(prefix="pb-refresh-eval-") as tmp:
        copied = Path(tmp) / "service.sqlite3"
        shutil.copyfile(original, copied)
        wal = Path(str(original) + "-wal")
        if wal.exists():
            shutil.copyfile(wal, str(copied) + "-wal")
        with contextlib.closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)) as svc:
            svc.row_factory = sqlite3.Row
            svc.execute("PRAGMA query_only=ON")
            now = args.now or svc.execute("SELECT max(fetched_at) FROM pb_raw_ref").fetchone()[0]
            if now is None:
                parser.error("empty pb_raw_ref: provide --now")
            try:
                timeutil.parse_iso(now)
            except ValueError as exc:
                parser.error(str(exc))
            print(json.dumps(evaluate(svc, now), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
