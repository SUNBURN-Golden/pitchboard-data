"""Private shadow ledger. Reservations are permanent ceiling charges, never refunds."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import fcntl
import os
from pathlib import Path
import sqlite3
import uuid

from .common import Stop, append_sync, dumps, loads, money, secure_open, shadow_root, sync_dir


def reservation(config):
    p = config["pricing"]
    return ((Decimal(p["input_token_reservation_ceiling"]) * money(p["input_usd_per_million_ceiling"]) +
             Decimal(config["max_output_tokens"]) * money(p["output_usd_per_million_ceiling"])) /
            Decimal(1000000)).quantize(Decimal("0.0000001"), rounding=ROUND_CEILING)


def comparison_output(root, extra_protected=()):
    """Validate the shadow root and return the report path.

    Score-only runs use this instead of ``State`` so they never create an
    experiment identity, lock, or budget ledger. The output itself is still
    confined to the validated shadow root.
    """
    return shadow_root(root, extra_protected) / "comparison.json"


class State:
    def __init__(self, root, extra_protected=()):
        self.root = shadow_root(root, extra_protected)

    @contextmanager
    def locked(self):
        root = self.root
        marker = root / "experiment.json"
        # Never adopt an unrelated populated directory, even if a lock is present.
        if not marker.exists() and any(root.iterdir()):
            raise Stop("unrecognized_shadow_root")
        with secure_open(root / "experiment.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with self._database() as conn:
                    self.recover(conn)
                    yield conn
            except (Stop, KeyboardInterrupt):
                raise
            except Exception:
                raise Stop("shadow_state_error") from None
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def _database(self):
        root = self.root
        marker, ledger = root / "experiment.json", root / "budget.sqlite3"
        fresh = not marker.exists()
        if fresh:
            if set(p.name for p in root.iterdir()) != {"experiment.lock"}:
                raise Stop("unrecognized_shadow_root")
            with secure_open(marker, "a+") as f:
                append_sync(f, {"schema": 1, "experiment_id": uuid.uuid4().hex})
        with secure_open(marker, "r", create=False) as f:
            identity = loads(f.read())
        if set(identity) != {"schema", "experiment_id"} or identity["schema"] != 1:
            raise Stop("invalid_experiment")
        if not fresh and not ledger.exists():
            raise Stop("missing_budget_state")
        with secure_open(ledger, "a+", create=fresh):
            pass
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(ledger) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                with secure_open(sidecar, "r", create=False):
                    pass
        # The only DB this module can open is this validated, private shadow path.
        conn = sqlite3.connect(str(ledger), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous=FULL")
            if fresh:
                conn.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE attempts(id TEXT PRIMARY KEY, dedup TEXT UNIQUE NOT NULL,
                  day TEXT NOT NULL, reserved_usd TEXT NOT NULL, result TEXT);
                """)
                conn.execute("INSERT INTO meta VALUES('experiment_id',?)", (identity["experiment_id"],))
                conn.execute("INSERT INTO meta VALUES('watermark','')")
                conn.execute("INSERT INTO meta VALUES('halted','')")
                conn.commit()
                sync_dir(root)
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise Stop("corrupt_budget_state")
            if conn.execute("SELECT value FROM meta WHERE key='experiment_id'").fetchone()[0] != identity["experiment_id"]:
                raise Stop("invalid_experiment")
            yield conn
        finally:
            conn.close()

    def output(self, day):
        path = self.root / "data"
        for path in (path, path / "shadow"):
            if path.is_symlink():
                raise Stop("unsafe_path")
            path.mkdir(mode=0o700, exist_ok=True)
            if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                raise Stop("unsafe_path")
        return self.root / "data/shadow" / ("llm_extract_" + day.replace("-", "") + ".jsonl")

    def recover(self, conn):
        """Replay only validated results; a partial output line fails closed."""
        for row in conn.execute("SELECT DISTINCT day FROM attempts WHERE result IS NOT NULL"):
            self.publish(conn, row["day"])

    def publish(self, conn, day):
        with secure_open(self.output(day), "a+") as f:
            f.seek(0)
            text = f.read()
            if text and not text.endswith("\n"):
                raise Stop("partial_shadow_output")
            ids = set()
            for line in text.splitlines():
                row = loads(line)
                if row["attempt_id"] in ids:
                    raise Stop("duplicate_shadow_output")
                ids.add(row["attempt_id"])
            for row in conn.execute("SELECT id,result FROM attempts WHERE day=? AND result IS NOT NULL ORDER BY rowid", (day,)):
                if row["id"] not in ids:
                    append_sync(f, loads(row["result"]))
            sync_dir(self.output(day).parent)

    def reserve(self, conn, config, dedup, now):
        time = now.isoformat()
        last = conn.execute("SELECT value FROM meta WHERE key='watermark'").fetchone()[0]
        if last and time < last:
            raise Stop("clock_backwards")
        if conn.execute("SELECT value FROM meta WHERE key='halted'").fetchone()[0]:
            raise Stop("experiment_halted")
        if conn.execute("SELECT 1 FROM attempts WHERE dedup=?", (dedup,)).fetchone():
            raise Stop("duplicate_page")
        day = now.date().isoformat()
        charge = reservation(config)
        costs = [money(r[0]) for r in conn.execute("SELECT reserved_usd FROM attempts WHERE day=?", (day,))]
        if len(costs) >= config["daily_call_cap"] or sum(costs, Decimal(0)) + charge > money(config["daily_cost_cap_usd"]):
            raise Stop("budget_exhausted")
        # Preflight output before the durable reservation. No network on failure.
        with secure_open(self.output(day), "a+"):
            pass
        attempt_id = uuid.uuid4().hex
        conn.execute("INSERT INTO attempts VALUES(?,?,?,?,NULL)", (attempt_id, dedup, day, str(charge)))
        conn.execute("UPDATE meta SET value=? WHERE key='watermark'", (time,))
        conn.commit()
        return attempt_id, day, str(charge)

    def finish(self, conn, record):
        if record["outcome"] in ("model_mismatch", "usage_invariant"):
            conn.execute("UPDATE meta SET value=? WHERE key='halted'", (record["outcome"],))
        cur = conn.execute("UPDATE attempts SET result=? WHERE id=? AND result IS NULL", (dumps(record), record["attempt_id"]))
        if cur.rowcount != 1:
            raise Stop("already_finished")
        conn.commit()
        self.publish(conn, record["budget_day"])
