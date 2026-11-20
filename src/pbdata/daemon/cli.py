"""python -m pbdata.daemon run|status|questions|retry  (data root outside the repo; never touches the live crawler)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .. import db, timeutil
from . import questions
from .runner import DaemonLock, Ledger, RunContext, StageRunner
from .stages import STAGES

DEFAULT_TARGETS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "targets", "pitchboard.json")


def open_dbs(root):
    for d in ("service_db", "infra_db", "locks", "exports", "inbox/manual_clips"):
        os.makedirs(os.path.join(root, d), mode=0o700, exist_ok=True)
    return (db.open_service(os.path.join(root, "service_db", "pitchboard_kg.sqlite3")),
            db.open_infra(os.path.join(root, "infra_db", "infra.sqlite3")))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pbdata.daemon")
    ap.add_argument("cmd", choices=["run", "status", "questions", "retry"])
    ap.add_argument("--root", default=os.environ.get("PB_DATA_ROOT", "/workspace/pitchboard/data-root"))
    ap.add_argument("--targets", default=DEFAULT_TARGETS)
    ap.add_argument("--run-id", default=None, help="reuse to resume from the failed stage")
    ap.add_argument("--max-jobs", type=int, help="maximum jobs to run (default: unlimited)")
    ap.add_argument("--caps", help='optional JSON request caps per lane, e.g. {"discovery":{"requests":150}}')
    ap.add_argument("--api-cap", type=int, help="maximum Wikimedia API calls (default: unlimited)")
    ap.add_argument("--refresh-limit", type=int, help="maximum refresh jobs to enqueue (default: unlimited)")
    ap.add_argument("--offline", action="store_true", help="skip network stages (recompute/verify only)")
    a = ap.parse_args(argv)
    os.environ["PB_DATA_ROOT"] = a.root
    svc, infra = open_dbs(a.root)
    if a.cmd == "questions":
        print(json.dumps(questions.answer_all(svc), ensure_ascii=False, indent=1))
        return 0
    if a.cmd == "status":
        print(json.dumps({"stage_run": [dict(r) for r in infra.execute("SELECT stage,status,attempts,run_id,error_code FROM stage_run ORDER BY started_at DESC LIMIT 12")],
                          "failure_queue_open": [dict(r) for r in infra.execute("SELECT stage,subject_id,error_code,attempts,resume_from FROM failure_queue WHERE state='open' LIMIT 30")],
                          "cost_today": [dict(r) for r in infra.execute("SELECT * FROM cost_ledger WHERE day=?", (time.strftime("%Y-%m-%d"),))]},
                         ensure_ascii=False, indent=1, default=str))
        return 0
    if a.cmd == "retry":   # operator re-queue: open daemon-stage failures become 'requeued'; the next run resumes them
        n = infra.execute("UPDATE failure_queue SET state='requeued', requeued_at=? WHERE state='open' AND stage IN (%s)" %
                          ",".join("'%s'" % s for s, _ in STAGES), (timeutil.iso(timeutil.utcnow()),)).rowcount
        print("requeued", n)
        return 0
    clock = lambda: timeutil.iso(timeutil.utcnow())
    run_id = a.run_id or "dmn_" + time.strftime("%Y%m%dT%H%M%S")
    caps = json.loads(a.caps) if a.caps else None
    opts = {"targets": json.load(open(a.targets)), "caps": caps, "max_jobs": a.max_jobs, "api_cap": a.api_cap,
            "refresh_limit": a.refresh_limit, "offline": a.offline,
            "run_tag": run_id, "quiet": True}
    ledger = Ledger(infra, time.strftime("%Y-%m-%d"))
    ctx = RunContext(svc, infra, a.root, run_id, clock, ledger, opts)
    with DaemonLock(os.path.join(a.root, "locks", "daemon.lock")), DaemonLock(os.path.join(a.root, "locks", "collector.lock")):
        rep = StageRunner(ctx, STAGES).run()
    rep["state"] = {k: v for k, v in ctx.state.items()}
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    return 0 if "stopped" not in rep else 2


if __name__ == "__main__":
    sys.exit(main())
