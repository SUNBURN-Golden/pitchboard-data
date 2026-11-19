"""One-off backfill: withdraw live future call deadlines that their own source's newest rules inspection no longer states.

Dry run by default (prints what would change). --apply writes; back up the service DB first.
Usage: PYTHONPATH=src python3 scripts/withdraw_unstated_deadlines.py DB [--apply] [--now 2026-11-20T00:00:00Z]
"""
import argparse
import json
import sqlite3
import sys

from pbdata import db, timeutil
from pbdata.collect import promote
from pbdata.profiles import pitchboard as prof


def latest_inspected_raw(svc, source_id):
    return svc.execute(
        "SELECT r.raw_id FROM pb_raw_ref r WHERE r.source_id=? AND r.outcome='ok' AND r.kind IN ('html','pdf','image') AND "
        "(EXISTS(SELECT 1 FROM pb_extraction e WHERE e.raw_id=r.raw_id AND e.extractor_version=?) OR "
        " EXISTS(SELECT 1 FROM pb_label l WHERE l.raw_id=r.raw_id AND l.labeler_version=?)) "
        "ORDER BY r.fetched_at DESC LIMIT 1", (source_id, prof.EXTRACTOR_VERSION, prof.LABELER_VERSION)).fetchone()


def kept_dates(svc, raw_id):
    rec = svc.execute("SELECT status FROM pb_label WHERE raw_id=? AND labeler_version=? AND label='recruitment'",
                      (raw_id, prof.LABELER_VERSION)).fetchone()
    if rec is not None and rec["status"] in promote.SKIP_LABEL_STATUS:
        return set()
    return {json.loads(r["value_json"])["date"] for r in svc.execute(
        "SELECT value_json FROM pb_extraction WHERE raw_id=? AND extractor_version=? AND field='deadline.closes_on'",
        (raw_id, prof.EXTRACTOR_VERSION))}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--now", default=None)
    a = ap.parse_args(argv)
    now = a.now or timeutil.iso(timeutil.utcnow())
    svc = db.connect(a.db)
    svc.row_factory = sqlite3.Row
    out = []
    srcs = [r[0] for r in svc.execute("SELECT DISTINCT source_id FROM pb_fact WHERE predicate='deadline.closes_on' AND subject_type='call' "
                                      "AND superseded_at IS NULL AND value_state='stated' AND json_extract(value_json,'$.date')>=?", (now[:10],))]
    for sid in srcs:
        raw = latest_inspected_raw(svc, sid)
        if raw is None:
            continue
        keep = kept_dates(svc, raw["raw_id"])
        live = svc.execute("SELECT f.fact_id, f.subject_id, json_extract(f.value_json,'$.date') d, s.canonical_url u FROM pb_fact f "
                           "JOIN pb_source s ON s.source_id=f.source_id WHERE f.source_id=? AND f.predicate='deadline.closes_on' "
                           "AND f.subject_type='call' AND f.superseded_at IS NULL AND f.value_state='stated' "
                           "AND json_extract(f.value_json,'$.date')>=?", (sid, now[:10])).fetchall()
        drop = [dict(r) for r in live if r["d"] not in keep]
        if not drop:
            continue
        if a.apply:
            n = promote.withdraw_unstated_deadlines(svc, raw["raw_id"], keep, now)
            svc.commit()
        else:
            n = 0
        out.append({"source_id": sid, "raw_id": raw["raw_id"], "kept": sorted(keep), "withdraw": drop, "applied": n})
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
