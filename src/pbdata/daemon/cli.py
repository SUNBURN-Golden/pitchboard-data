"""python -m pbdata.daemon run|status|questions|retry  (data root outside the repo; never touches the live crawler)."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

from .. import db, timeutil
from . import questions
from ..collect.scheduler import wait_until_free
from .runner import DaemonLock, Ledger, RunContext, StageRunner
from .stages import STAGES

# Wall-clock budget for stage 2 (incremental fetch / wiki discovery jobs). Without it one run crawled a whole site
# frontier for 12h+ while holding collector.lock, so the 04:20 collector failed every day. Unfinished jobs stay queued.
DEFAULT_FETCH_BUDGET_S = 90 * 60

# The daemon waits (instead of failing) while daily.sh holds daily.lock or another run holds daemon/collector.lock,
# and its fetch stage stops leasing before the next 04:20 daily window, so a manual or late run never holds
# collector.lock when the 04:20 collector starts (2026-11-26: a run started 11-25 22:24 KST held it until 07:34).
DEFAULT_LOCK_WAIT_S = 4 * 3600
DAILY_WINDOW = os.environ.get("PB_DAILY_WINDOW", "04:20")            # box-local time daily.sh starts (crontab)
WINDOW_MARGIN_S = float(os.environ.get("PB_DAEMON_WINDOW_MARGIN_MIN", "30")) * 60


def next_window_stop(now: float, window: str = None, margin_s: float = None) -> float:
    """Epoch seconds at which the fetch stage must stop leasing: margin_s before the next daily window start.
    Between (window - margin) and window this is in the past, so the fetch stage leases nothing."""
    window = window or DAILY_WINDOW
    margin_s = WINDOW_MARGIN_S if margin_s is None else margin_s
    hh, mm = (int(x) for x in window.split(":"))
    lt = time.localtime(now)
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if start <= now:
        lt2 = time.localtime(now + 86400)
        start = time.mktime((lt2.tm_year, lt2.tm_mon, lt2.tm_mday, hh, mm, 0, 0, 0, -1))
    return start - margin_s

DEFAULT_TARGETS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "targets", "pitchboard.json")


def open_dbs(root):
    for d in ("service_db", "infra_db", "locks", "exports", "inbox/manual_clips"):
        os.makedirs(os.path.join(root, d), mode=0o700, exist_ok=True)
    return (db.open_service(os.path.join(root, "service_db", "pitchboard_kg.sqlite3")),
            db.open_infra(os.path.join(root, "infra_db", "infra.sqlite3")))


def _exit_on_sigterm():
    """timeout(1)/kill send SIGTERM: unwind normally so open transactions roll back and both lock files are released."""
    def handler(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, handler)


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
    ap.add_argument("--fetch-budget", type=float, default=float(os.environ.get("PB_DAEMON_FETCH_BUDGET_S", DEFAULT_FETCH_BUDGET_S)),
                    help="wall-clock seconds for the incremental fetch stage (0 = unlimited; default %(default)s)")
    ap.add_argument("--offline", action="store_true", help="skip network stages (recompute/verify only)")
    ap.add_argument("--lock-wait", type=float, default=float(os.environ.get("PB_DAEMON_LOCK_WAIT_S", DEFAULT_LOCK_WAIT_S)),
                    help="seconds to wait for daily.lock / daemon.lock / collector.lock (default %(default)s)")
    ap.add_argument("--no-window-stop", action="store_true", help="do not stop the fetch stage before the 04:20 daily window")
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
            "refresh_limit": a.refresh_limit, "offline": a.offline, "fetch_budget_s": a.fetch_budget or None,
            "run_tag": run_id, "quiet": True}
    _exit_on_sigterm()
    locks = os.path.join(a.root, "locks")
    t0 = time.time()
    if not wait_until_free(os.path.join(locks, "daily.lock"), a.lock_wait):
        print("daily.lock still held after %ds; daemon not started" % a.lock_wait, file=sys.stderr)
        return 3
    left = max(0.0, a.lock_wait - (time.time() - t0))
    with DaemonLock(os.path.join(locks, "daemon.lock"), left), DaemonLock(os.path.join(locks, "collector.lock"), left):
        if not a.no_window_stop:
            opts["stop_at"] = next_window_stop(time.time())
        ledger = Ledger(infra, time.strftime("%Y-%m-%d"))
        ctx = RunContext(svc, infra, a.root, run_id, clock, ledger, opts)
        rep = StageRunner(ctx, STAGES).run()
    rep["state"] = {k: v for k, v in ctx.state.items()}
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    return 0 if "stopped" not in rep else 2


if __name__ == "__main__":
    sys.exit(main())
