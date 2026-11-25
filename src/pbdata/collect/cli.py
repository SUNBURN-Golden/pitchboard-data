"""CLI: python -m pbdata.collect run|status|metrics --root DIR --targets FILE"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .. import db, timeutil
from . import collector, metrics
from .net import Fetcher
from .scheduler import RunLock
from .wiki import Api


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pbdata.collect")
    ap.add_argument("cmd", choices=["run", "refresh", "metrics", "status"])
    ap.add_argument("--root", default=os.environ.get("PB_DATA_ROOT", "/workspace/pitchboard/data-root"))
    ap.add_argument("--targets", default=os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "targets", "pitchboard.json"))
    ap.add_argument("--run-tag", default=time.strftime("%Y%m%dT%H%M%S"))
    ap.add_argument("--run-id", default=None, help="reuse to resume a run")
    ap.add_argument("--max-jobs", type=int, help="maximum jobs to run (default: unlimited)")
    ap.add_argument("--no-seed", action="store_true", help="resume: do not enqueue discovery jobs")
    ap.add_argument("--caps", help='optional JSON request caps per lane, e.g. {"discovery":{"requests":150}}')
    ap.add_argument("--api-cap", type=int, help="maximum Wikimedia API calls (default: unlimited)")
    ap.add_argument("--time-budget", type=float, help="wall-clock seconds for the job loop; unfinished jobs stay queued (default: unlimited)")
    ap.add_argument("--refresh-limit", type=int, help="maximum refresh jobs to enqueue (default: unlimited)")
    a = ap.parse_args(argv)
    os.environ["PB_DATA_ROOT"] = a.root
    for d in ("service_db", "infra_db", "locks"):
        os.makedirs(os.path.join(a.root, d), mode=0o700, exist_ok=True)
    svc = db.open_service(os.path.join(a.root, "service_db", "pitchboard_kg.sqlite3"))
    infra = db.open_infra(os.path.join(a.root, "infra_db", "infra.sqlite3"))
    targets = json.load(open(a.targets))
    if a.cmd == "status":
        print(json.dumps({"jobs": dict(infra.execute("SELECT state, count(*) FROM job GROUP BY state").fetchall()), "checkpoints": [dict(r) for r in infra.execute("SELECT run_id,name,cursor FROM checkpoint")]}, indent=1))
        return 0
    if a.cmd == "metrics":
        print(json.dumps(metrics.compute(svc, infra, targets), indent=2, ensure_ascii=False))
        return 0
    clock = lambda: timeutil.iso(timeutil.utcnow())
    run_id = a.run_id or "run_" + a.run_tag
    ctx = collector.Ctx(svc, infra, Fetcher(infra), Api(), targets, run_id, a.run_tag, clock,
                        json.loads(a.caps) if a.caps else None, api_cap=a.api_cap)
    with RunLock(os.path.join(a.root, "locks", "collector.lock")):
        if a.cmd == "run" and not a.no_seed:
            print("seeded discover jobs:", collector.seed_discovery(ctx))
        if a.cmd == "refresh":
            print("refresh jobs:", collector.enqueue_refresh(ctx, a.refresh_limit))
            print("retry jobs:", collector.enqueue_retries(ctx))
        stats = collector.run(ctx, a.max_jobs, time_budget_s=a.time_budget)
    print(json.dumps({"run_id": run_id, "stats": stats, "requests": ctx.fetcher.requests, "api_calls": ctx.api.calls}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
