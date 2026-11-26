"""Stage runner: lock, kill switch, per-stage checkpoints (infra.stage_run), bounded retries, failure queue,
usage ledger, and resume from the failed stage (same run_id)."""
from __future__ import annotations

import fcntl
import json
import os
import time
import traceback
from typing import Callable, Dict, List, Tuple

from ..ids import sha256_hex

MAX_ATTEMPTS = 3
BACKOFF_S = (0, 2, 8)


class NonRetryable(Exception):
    pass


class DaemonLock:
    def __init__(self, path: str, wait_s: float = 0):
        self.path, self.fh, self.wait_s = path, None, wait_s

    def __enter__(self):
        from ..collect.scheduler import acquire_flock
        self.fh = acquire_flock(self.path, self.wait_s)
        if self.fh is None:
            raise RuntimeError("another daemon run holds " + self.path)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


class Ledger:
    """Daily usage accounting (infra.cost_ledger)."""
    def __init__(self, infra, day: str):
        self.infra, self.day = infra, day

    def used(self, resource: str) -> float:
        r = self.infra.execute("SELECT amount FROM cost_ledger WHERE day=? AND resource=?", (self.day, resource)).fetchone()
        return r[0] if r else 0.0

    def charge(self, resource: str, amount: float) -> None:
        self.infra.execute("INSERT INTO cost_ledger(day,resource,amount) VALUES(?,?,?) ON CONFLICT(day,resource) DO UPDATE SET amount=amount+excluded.amount",
                           (self.day, resource, amount))
        self.infra.commit()

StageFn = Callable[["RunContext"], dict]


class RunContext:
    def __init__(self, svc, infra, root: str, run_id: str, clock: Callable[[], str], ledger: Ledger, opts: dict):
        self.svc, self.infra, self.root, self.run_id, self.clock, self.ledger, self.opts = svc, infra, root, run_id, clock, ledger, opts
        self.state: Dict[str, dict] = {}     # outputs of earlier stages in this run (reloaded from checkpoints on resume)
        self.log: Callable[[str], None] = lambda line: print(line, flush=True)


class StageRunner:
    def __init__(self, ctx: RunContext, stages: List[Tuple[str, StageFn]], sleep=time.sleep):
        self.ctx, self.stages, self.sleep = ctx, stages, sleep

    def _key(self, stage: str) -> str:
        return sha256_hex("%s|%s|%s" % (stage, self.ctx.run_id, self.ctx.opts.get("input_digest", "")))

    def _row(self, key: str):
        return self.ctx.infra.execute("SELECT * FROM stage_run WHERE run_key=?", (key,)).fetchone()

    def _kill(self) -> bool:
        return os.path.exists(os.path.join(self.ctx.root, "locks", "KILL_SWITCH"))

    def _fail(self, stage: str, code: str, detail: str, attempts: int, now: str) -> None:
        fid = sha256_hex("fq|%s|%s" % (stage, self.ctx.run_id))[:24]
        self.ctx.infra.execute(
            "INSERT INTO failure_queue(failure_id,stage,subject_id,input_digest,error_code,error_detail,first_seen,last_seen,attempts,resume_from)"
            " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(stage,subject_id,input_digest) DO UPDATE SET last_seen=excluded.last_seen,"
            " attempts=failure_queue.attempts+excluded.attempts, error_code=excluded.error_code, error_detail=excluded.error_detail, state='open'",
            (fid, stage, self.ctx.run_id, self.ctx.opts.get("input_digest", ""), code, detail[-1500:], now, now, attempts, stage))
        self.ctx.infra.commit()

    def run(self) -> dict:
        c = self.ctx
        report = {"run_id": c.run_id, "stages": {}}
        c.infra.execute("INSERT OR IGNORE INTO runs(run_id,started_at) VALUES(?,?)", (c.run_id, c.clock()))
        c.infra.commit()
        for stage, fn in self.stages:
            if self._kill():
                report["stopped"] = "kill_switch"
                break
            key = self._key(stage)
            row = self._row(key)
            if row is not None and row["status"] == "done":      # resume: skip completed stages, reload their output
                c.state[stage] = json.loads(row["output_digest"] or "{}")
                report["stages"][stage] = {"status": "skipped_done"}
                continue
            attempts = row["attempts"] if row is not None and row["status"] in ("running", "retry_wait") else 0
            outcome = None
            while attempts < MAX_ATTEMPTS:
                now = c.clock()
                attempts += 1
                c.infra.execute("INSERT INTO stage_run(run_key,stage,subject_id,input_digest,status,attempts,started_at,run_id) VALUES(?,?,?,?,?,?,?,?)"
                                " ON CONFLICT(run_key) DO UPDATE SET status='running', attempts=excluded.attempts, started_at=excluded.started_at",
                                (key, stage, c.run_id, c.opts.get("input_digest", ""), "running", attempts, now, c.run_id))
                c.infra.commit()
                try:
                    if os.environ.get("PB_DAEMON_CRASH_AT") == stage:
                        os._exit(137)                                  # crash injection for the resume proof
                    out = fn(c) or {}
                    c.state[stage] = out
                    c.infra.execute("UPDATE stage_run SET status='done', output_digest=?, finished_at=?, error_code=NULL WHERE run_key=?",
                                    (json.dumps(out, sort_keys=True, ensure_ascii=False, default=str), c.clock(), key))
                    c.infra.commit()
                    outcome = {"status": "done", "attempts": attempts, **{k: v for k, v in out.items() if not isinstance(v, (list, dict))}}
                    break
                except NonRetryable as e:
                    c.svc.rollback() if c.svc.in_transaction else None
                    self._fail(stage, type(e).__name__, str(e), attempts, c.clock())
                    c.infra.execute("UPDATE stage_run SET status='failed', error_code=? WHERE run_key=?", (type(e).__name__, key))
                    c.infra.commit()
                    outcome = {"status": "failed", "error": str(e)}
                    break
                except Exception as e:  # bounded retry with backoff
                    c.svc.rollback() if c.svc.in_transaction else None
                    detail = traceback.format_exc()
                    c.infra.execute("UPDATE stage_run SET status=?, error_code=? WHERE run_key=?",
                                    ("retry_wait" if attempts < MAX_ATTEMPTS else "failed", type(e).__name__, key))
                    c.infra.commit()
                    if attempts >= MAX_ATTEMPTS:
                        self._fail(stage, type(e).__name__, detail, attempts, c.clock())
                        outcome = {"status": "failed", "error": "%s: %s" % (type(e).__name__, e)}
                        break
                    self.sleep(BACKOFF_S[min(attempts, len(BACKOFF_S) - 1)])
            report["stages"][stage] = outcome
            c.log("[daemon] %-20s %s" % (stage, json.dumps(outcome, ensure_ascii=False, default=str)[:300]))
            if outcome and outcome["status"] == "failed":
                report["stopped"] = "failed_at:" + stage      # later stages depend on this one; next run resumes here
                break
        c.infra.execute("UPDATE runs SET finished_at=?, outcome=?, report=? WHERE run_id=?",
                        (c.clock(), report.get("stopped", "ok"), json.dumps(report, default=str), c.run_id))
        c.infra.commit()
        return report
