"""Job queue + priority + separate budgets (backfill / discovery / freshness). Jobs live in the infra DB, are leased,
idempotent (idem_key), retried with bounded backoff, and resumed after a crash (expired leases return to pending)."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import random
import sqlite3
from typing import Dict, List, Optional

from .. import timeutil
from ..db import transaction
from ..ids import new_id

BUDGET_CLASSES = ("backfill", "discovery", "freshness")
MAX_ATTEMPTS = 3
LEASE_SECONDS = 300
WEIGHTS = dict(novelty=0.30, gap=0.25, urgency=0.20, quality=0.15, cost=0.10, duplication=0.15, failure=0.20)
# Refetch of a source backing a live open call that is due (expired fact or older-labeler label). Above the
# largest value priority() can return (sum of positive weights = 0.90), so it is leased before any other work.
LIVE_CALL_REFRESH_PRIORITY = 1.0


def priority(novelty=0.0, gap_importance=0.0, urgency=0.0, quality=0.0, cost=0.0, duplication=0.0, failures=0) -> float:
    """Higher first. Inputs 0..1. Repeated failures push work down; known duplicates and cost push down."""
    c = lambda x: max(0.0, min(1.0, float(x)))
    w = WEIGHTS
    score = (w["novelty"] * c(novelty) + w["gap"] * c(gap_importance) + w["urgency"] * c(urgency) + w["quality"] * c(quality)
             - w["cost"] * c(cost) - w["duplication"] * c(duplication) - w["failure"] * c(failures / 3.0))
    return round(score, 4)


def urgency_from_deadline(closes_on: Optional[str], today: str) -> float:
    if not closes_on:
        return 0.3
    days = (dt.date.fromisoformat(closes_on) - dt.date.fromisoformat(today)).days
    if days < 0:
        return 0.0
    return round(max(0.1, 1.0 - days / 60.0), 3)


def _try_flock(fh) -> bool:
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def acquire_flock(path: str, wait_s: float = 0, sleep=None, clock=None):
    """Open path and take an exclusive flock, polling for up to wait_s wall-clock seconds. Returns the open file or None."""
    import time as _t
    sleep, clock = sleep or _t.sleep, clock or _t.time
    fh = open(path, "a+")
    end = clock() + max(0.0, wait_s or 0)
    while not _try_flock(fh):
        if clock() >= end:
            fh.close()
            return None
        sleep(min(1.0, max(0.05, end - clock())))
    return fh


def wait_until_free(path: str, wait_s: float = 0, sleep=None, clock=None) -> bool:
    """True once nobody holds path (checked by taking and releasing the lock), False after wait_s."""
    fh = acquire_flock(path, wait_s, sleep, clock)
    if fh is None:
        return False
    fcntl.flock(fh, fcntl.LOCK_UN)
    fh.close()
    return True


class RunLock:
    """Single-run lock (flock) so two collectors never run concurrently on the same infra DB.
    wait_s > 0 waits (outside any job-loop time budget) for the holder to finish instead of failing at once."""
    def __init__(self, path: str, wait_s: float = 0):
        self.path = path
        self.wait_s = wait_s
        self.fh = None

    def __enter__(self):
        self.fh = acquire_flock(self.path, self.wait_s)
        if self.fh is None:
            raise RuntimeError("another collector run holds the lock: " + self.path)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def enqueue(infra: sqlite3.Connection, kind: str, budget_class: str, payload: dict, idem: str, prio: float, now: str,
            depth: int = 0, parent: Optional[str] = None) -> Optional[str]:
    assert budget_class in BUDGET_CLASSES
    jid = new_id("job", int(timeutil.parse_iso(now).timestamp() * 1000))
    cur = infra.execute("INSERT OR IGNORE INTO job(job_id,idem_key,kind,budget_class,payload,priority,state,depth,parent_job_id,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (jid, idem, kind, budget_class, json.dumps(payload, sort_keys=True, ensure_ascii=False),
                                                         prio, "pending", depth, parent, now, now))
    return jid if cur.rowcount else None


def requeue_expired(infra: sqlite3.Connection, now: str) -> int:
    """Resume: leased jobs whose lease expired (crashed worker) go back to pending, attempts preserved."""
    cur = infra.execute("UPDATE job SET state='pending', lease_expires=NULL, updated_at=? WHERE state='leased' AND lease_expires<?", (now, now))
    return cur.rowcount


def budget_used(infra, day: str, cls: str, resource: str) -> int:
    r = infra.execute("SELECT used FROM budget_ledger WHERE day=? AND budget_class=? AND resource=?", (day, cls, resource)).fetchone()
    return r["used"] if r else 0


def budget_add(infra, day: str, cls: str, resource: str, n: int) -> None:
    infra.execute("INSERT INTO budget_ledger(day,budget_class,resource,used) VALUES(?,?,?,?) ON CONFLICT(day,budget_class,resource) DO UPDATE SET used=used+?",
                  (day, cls, resource, n, n))


def lease_next(infra: sqlite3.Connection, now: str, allowed_classes: List[str], prefer: Optional[str] = None) -> Optional[sqlite3.Row]:
    """Lease the highest-priority due job. With prefer (a budget class), that class is tried first and the others
    only when it has nothing due: the run loop uses this to give freshness a fixed share of leases, because computed
    refresh priorities (<= 0.285) sit below discovery/backfill (0.3-0.37) and starved for days (2026-11-26: 3,714
    refresh URLs pending, 3 jobs done in a run)."""
    if not allowed_classes:
        return None
    order = [[prefer], allowed_classes] if prefer in allowed_classes else [allowed_classes]
    with transaction(infra):
        row = None
        for classes in order:
            q = ",".join("?" * len(classes))
            row = infra.execute("SELECT * FROM job WHERE state IN ('pending','retry_wait') AND (next_at IS NULL OR next_at<=?) AND budget_class IN (%s) "
                                "ORDER BY priority DESC, created_at LIMIT 1" % q, [now] + list(classes)).fetchone()
            if row is not None:
                break
        if row is None:
            return None
        exp = timeutil.iso(timeutil.parse_iso(now) + dt.timedelta(seconds=LEASE_SECONDS))
        infra.execute("UPDATE job SET state='leased', lease_expires=?, attempts=attempts+1, updated_at=? WHERE job_id=?", (exp, now, row["job_id"]))
        return infra.execute("SELECT * FROM job WHERE job_id=?", (row["job_id"],)).fetchone()


def dedupe_key(kind: str, payload: dict) -> Optional[str]:
    """What makes two queued jobs the same work regardless of the run tag in their idem_key."""
    if kind == "fetch_page":
        return "fetch|%s|%s" % (payload.get("url"), payload.get("via") or "")
    if kind == "discover":
        return "discover|%s|%s|%s" % (payload.get("target_id"), payload.get("strategy"), payload.get("name") or "")
    if kind == "evaluate":
        return "evaluate|%s" % payload.get("cand_id")
    if kind in ("gap_fill", "expand_site"):
        return "%s|%s" % (kind, json.dumps(payload, sort_keys=True))
    return None


def queued_keys(infra: sqlite3.Connection, kind: str, budget_class: Optional[str] = None) -> Dict[str, tuple]:
    """dedupe_key -> (job_id, priority) of every pending/retry_wait/leased job of this kind."""
    sql = "SELECT job_id, kind, payload, priority FROM job WHERE kind=? AND state IN ('pending','retry_wait','leased')"
    args = [kind]
    if budget_class:
        sql += " AND budget_class=?"
        args.append(budget_class)
    out = {}
    for r in infra.execute(sql, args):
        k = dedupe_key(r["kind"], json.loads(r["payload"]))
        if k is not None and (k not in out or r["priority"] > out[k][1]):
            out[k] = (r["job_id"], r["priority"])
    return out


def collapse_duplicates(infra: sqlite3.Connection, now: str) -> int:
    """Queued copies of the same work (same URL / target+strategy / candidate, enqueued again by a later run under
    a new run tag) become done with result superseded_by; the copy kept is the highest-priority, oldest one.
    Leased jobs are never touched. 2026-11-26: 18,663 pending refresh jobs were 3,714 URLs x 6 run tags."""
    keep: Dict[str, sqlite3.Row] = {}
    drop = []
    rows = infra.execute("SELECT job_id, kind, payload, priority, created_at, state FROM job WHERE state IN ('pending','retry_wait','leased') "
                         "ORDER BY (state='leased') DESC, priority DESC, created_at, job_id").fetchall()
    for r in rows:
        k = dedupe_key(r["kind"], json.loads(r["payload"]))
        if k is None:
            continue
        if k in keep:
            if r["state"] != "leased":
                drop.append((json.dumps({"superseded_by": keep[k]["job_id"]}), now, r["job_id"]))
        else:
            keep[k] = r
    if drop:
        with transaction(infra):
            infra.executemany("UPDATE job SET state='done', result=?, lease_expires=NULL, updated_at=? WHERE job_id=? "
                              "AND state IN ('pending','retry_wait')", drop)
    return len(drop)


def finish(infra, job_id: str, result: dict, now: str) -> None:
    infra.execute("UPDATE job SET state='done', result=?, lease_expires=NULL, updated_at=? WHERE job_id=?", (json.dumps(result, sort_keys=True), now, job_id))


def fail(infra, job: sqlite3.Row, err: str, now: str, retryable: bool = True) -> str:
    if not retryable or job["attempts"] >= MAX_ATTEMPTS:
        infra.execute("UPDATE job SET state='dead', last_error=?, lease_expires=NULL, updated_at=? WHERE job_id=?", (err[:300], now, job["job_id"]))
        infra.execute("INSERT OR REPLACE INTO failure_queue(failure_id,stage,subject_id,input_digest,error_code,error_detail,first_seen,last_seen,attempts,resume_from) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)", (new_id("fail"), job["kind"], job["job_id"], job["idem_key"], err.split(":")[0][:60], err[:300], now, now, job["attempts"], job["payload"]))
        return "dead"
    delay = min(3600, 30 * (2 ** (job["attempts"] - 1))) + random.randint(0, 10)
    nxt = timeutil.iso(timeutil.parse_iso(now) + dt.timedelta(seconds=delay))
    infra.execute("UPDATE job SET state='retry_wait', next_at=?, last_error=?, lease_expires=NULL, updated_at=? WHERE job_id=?", (nxt, err[:300], now, job["job_id"]))
    return "retry_wait"


def checkpoint(infra, run_id: str, name: str, cursor: dict, now: str) -> None:
    infra.execute("INSERT INTO checkpoint(run_id,name,cursor,updated_at) VALUES(?,?,?,?) ON CONFLICT(run_id,name) DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at",
                  (run_id, name, json.dumps(cursor, sort_keys=True), now))
