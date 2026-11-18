#!/usr/bin/env python3
"""Evaluate junk-source retirement on a temporary copy of a service SQLite database."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pbdata import db
from pbdata.collect import metrics, store
from pbdata.collect.net import junk_page_url


def evaluate(svc) -> dict:
    before = metrics.source_fetch_counts(svc)
    already_retired = {r[0] for r in svc.execute("SELECT source_id FROM pb_source WHERE retired_reason IS NOT NULL")}
    held_out = svc.execute(
        "SELECT s.source_id, s.canonical_url FROM pb_source s WHERE s.last_success_at IS NOT NULL "
        "OR EXISTS (SELECT 1 FROM pb_raw_ref r WHERE r.source_id=s.source_id AND r.outcome='ok') "
        "OR EXISTS (SELECT 1 FROM pb_fact f WHERE f.source_id=s.source_id) ORDER BY s.canonical_url"
    ).fetchall()

    retired_count = store.retire_junk_sources(svc)
    after = metrics.source_fetch_counts(svc)
    retired = {r["source_id"]: r for r in svc.execute(
        "SELECT source_id, canonical_url, retired_reason FROM pb_source WHERE retired_reason IS NOT NULL ORDER BY canonical_url"
    ) if r["source_id"] not in already_retired}
    flagged = [{"url": r["canonical_url"], "reason": reason} for r in held_out
               if (reason := junk_page_url(r["canonical_url"]))]
    return {
        "sources_total": svc.execute("SELECT count(*) FROM pb_source").fetchone()[0],
        "never_fetched_before": before["sources_never_fetched"],
        "never_fetched_after": after["sources_never_fetched"],
        "retired_count": retired_count,
        "retired_by_reason": dict(Counter(r["retired_reason"] for r in retired.values())),
        "retired_urls": [r["canonical_url"] for r in retired.values()],
        "held_out": {
            "sources_total": len(held_out),
            "flagged_count": len(flagged),
            "flagged_urls": flagged,
            "newly_retired_count": sum(r["source_id"] in retired for r in held_out),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service_sqlite", type=Path)
    original = parser.parse_args().service_sqlite.resolve()
    with TemporaryDirectory(prefix="pb-junk-sources-eval-") as tmp:
        copied = Path(tmp) / "service.sqlite3"
        shutil.copyfile(original, copied)
        wal = Path(str(original) + "-wal")
        if wal.is_file():
            shutil.copyfile(wal, str(copied) + "-wal")
        with closing(db.open_service(copied)) as svc:
            print(json.dumps(evaluate(svc), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
