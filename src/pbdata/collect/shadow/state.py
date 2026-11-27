"""Private shadow attempts ledger with durable dispatch accounting."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, DecimalException, ROUND_CEILING
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from .common import (Stop, append_sync, dumps, loads, no_symlinks, pricing_at,
                     secure_open, shadow_root, sqlite_snapshot, sync_dir, total_cost)


def reservation(config, now, provider):
    """Optional legacy ceiling estimate for logging a dispatch before usage arrives."""
    p = config.get({"qwen": "qwen_pricing", "codex": "codex_pricing"}.get(provider, "pricing"))
    rates = pricing_at(p, now)
    if rates is None or "input_token_reservation_ceiling" not in p:
        return None
    try:
        return ((Decimal(p["input_token_reservation_ceiling"]) * rates[0] +
                 Decimal(config["max_output_tokens"]) * rates[1]) /
                Decimal(1000000)).quantize(Decimal("0.0000001"), rounding=ROUND_CEILING)
    except DecimalException:
        return None


def read_history(root):
    """Read completed attempts from an initialized private ledger, read-only.

    Used by plan-only inspection (F3) so counts account for prior static skips
    without initializing a fresh root, taking a lock, or writing anything.
    """
    path = no_symlinks(root)
    if not path.exists():
        return []
    if not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise Stop("unsafe_root")
    marker, ledger = path / "experiment.json", path / "budget.sqlite3"
    if not marker.exists() and not ledger.exists():
        try:
            populated = any(path.iterdir())
        except OSError:
            raise Stop("unsafe_root") from None
        if populated:
            raise Stop("unrecognized_shadow_root")
        return []
    if not marker.exists() or not ledger.exists():
        raise Stop("unrecognized_shadow_root")
    with secure_open(marker, "r", create=False) as f:
        identity = loads(f.read())
    if set(identity) != {"schema", "experiment_id"} or identity["schema"] != 1:
        raise Stop("invalid_experiment")
    info = ledger.stat()
    if not stat.S_ISREG(info.st_mode):
        raise Stop("unsafe_file")
    if info.st_size > 256_000_000:
        raise Stop("budget_state_too_large")
    try:
        fd = os.open(ledger, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Stop("unsafe_path") from None
    try:
        f = os.fdopen(fd, "rb")
        fd = -1
        with f:
            raw = f.read(256_000_001)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > 256_000_000:
        raise Stop("budget_state_too_large")
    if raw[:16] != b"SQLite format 3\x00" or raw[18:20] != b"\x01\x01":
        raise Stop("corrupt_budget_state")
    try:
        with sqlite_snapshot(raw) as conn:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise Stop("corrupt_budget_state")
            if conn.execute("SELECT value FROM meta WHERE key='experiment_id'").fetchone()[0] != identity["experiment_id"]:
                raise Stop("invalid_experiment")
            return [loads(r[0]) for r in conn.execute(
                "SELECT result FROM attempts WHERE result IS NOT NULL ORDER BY rowid")]
    except sqlite3.Error:
        raise Stop("corrupt_budget_state") from None


def read_fetch_skips(root, max_files=60):
    """Read the private per-URL fetch-block log (newest files first), read-only."""
    path = no_symlinks(root)
    folder = path / "data" / "shadow"
    if not folder.is_dir() or folder.is_symlink():
        return []
    rows = []
    for log in sorted(folder.glob("fetch_skips_*.jsonl"), reverse=True)[:max_files]:
        with secure_open(log, "r", create=False) as f:
            for line in f:
                try:
                    row = loads(line)
                except Exception:
                    continue
                if isinstance(row, dict) and isinstance(row.get("url"), str) and isinstance(row.get("day"), str):
                    rows.append(row)
    return rows


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
                  day TEXT NOT NULL, reserved_usd TEXT NOT NULL, result TEXT, provider_calls INTEGER);
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
            if "provider_calls" not in {row["name"] for row in conn.execute("PRAGMA table_info(attempts)")}:
                conn.execute("ALTER TABLE attempts ADD COLUMN provider_calls INTEGER")
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
        self.publish_derived(conn, day)

    def publish_derived(self, conn, day):
        rows = [loads(r[0]) for r in conn.execute(
            "SELECT result FROM attempts WHERE day=? AND result IS NOT NULL ORDER BY rowid", (day,))]
        self.append_unique(day, "review_conflicts", "review_id", [v for r in rows for v in r.get("reviews", [])])
        self.append_unique(day, "digest_drafts", "draft_id", [v for r in rows for v in r.get("digest_drafts", [])])

    def append_unique(self, day, stream, id_key, rows):
        if not rows:
            return
        path = self.output(day).with_name(stream + "_" + day.replace("-", "") + ".jsonl")
        with secure_open(path, "a+") as f:
            f.seek(0)
            text = f.read()
            if text and not text.endswith("\n"):
                raise Stop("partial_shadow_output")
            existing = {}
            for line in text.splitlines():
                row = loads(line)
                if row[id_key] in existing:
                    raise Stop("duplicate_shadow_output")
                existing[row[id_key]] = row
            for row in rows:
                if row[id_key] in existing:
                    if row != existing[row[id_key]]:
                        raise Stop("conflicting_shadow_output")
                else:
                    append_sync(f, row)
                    existing[row[id_key]] = row
        sync_dir(path.parent)

    def preflight(self, conn, now):
        time = now.isoformat()
        last = conn.execute("SELECT value FROM meta WHERE key='watermark'").fetchone()[0]
        if last and time < last:
            raise Stop("clock_backwards")
        if conn.execute("SELECT value FROM meta WHERE key='halted'").fetchone()[0]:
            raise Stop("experiment_halted")

    def reserve(self, conn, dedup, now):
        self.preflight(conn, now)
        if conn.execute("SELECT 1 FROM attempts WHERE dedup=?", (dedup,)).fetchone():
            raise Stop("duplicate_page")
        time, day = now.isoformat(), now.date().isoformat()
        # Preflight output before the durable reservation. No network on failure.
        with secure_open(self.output(day), "a+"):
            pass
        attempt_id = uuid.uuid4().hex
        conn.execute("INSERT INTO attempts(id,dedup,day,reserved_usd,provider_calls) VALUES(?,?,?,?,0)",
                     (attempt_id, dedup, day, "0"))
        conn.execute("UPDATE meta SET value=? WHERE key='watermark'", (time,))
        conn.commit()
        return attempt_id, day

    def record_call(self, conn, config, now, attempt_id, provider):
        """Durably count each dispatch even if its result is lost."""
        self.preflight(conn, now)
        row = conn.execute("SELECT reserved_usd, result FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None or row["result"] is not None:
            raise Stop("shadow_state_error")
        charge = reservation(config, now, provider)
        prior = row["reserved_usd"]
        new_reserved = total_cost((None if prior == "unknown" else prior, None if charge is None else str(charge)))
        conn.execute("UPDATE attempts SET reserved_usd=?,provider_calls=provider_calls+1 WHERE id=?",
                     ("unknown" if new_reserved is None else new_reserved, attempt_id))
        conn.execute("UPDATE meta SET value=? WHERE key='watermark'", (now.isoformat(),))
        conn.commit()
        return new_reserved

    def finish(self, conn, record):
        if record["outcome"] in ("model_mismatch", "usage_invariant"):
            conn.execute("UPDATE meta SET value=? WHERE key='halted'", (record["outcome"],))
        cur = conn.execute("UPDATE attempts SET result=? WHERE id=? AND result IS NULL", (dumps(record), record["attempt_id"]))
        if cur.rowcount != 1:
            raise Stop("already_finished")
        conn.commit()
        self.publish(conn, record["budget_day"])
