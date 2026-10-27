"""SQLite access: connections, additive-only migrations with checksums, transactions."""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterator

from . import timeutil

DEFAULT_DATA_ROOT = "/workspace/pitchboard/data-root"
FORBIDDEN_SQL = re.compile(
    r"\b(DROP\s+(TABLE|COLUMN|INDEX|TRIGGER|VIEW)|DELETE\s+FROM|TRUNCATE|RENAME\s+(TO|COLUMN)|"
    r"ALTER\s+TABLE\s+\w+\s+DROP|UPDATE\s+\w+\s+SET)\b", re.I)
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "schema" / "migrations"


class MigrationError(Exception):
    pass


def data_root() -> Path:
    return Path(os.environ.get("PB_DATA_ROOT", DEFAULT_DATA_ROOT))


def connect(path: str | os.PathLike, readonly: bool = False) -> sqlite3.Connection:
    if str(path) == ":memory:":
        conn = sqlite3.connect(":memory:", isolation_level=None)
    elif readonly:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, isolation_level=None)
    else:
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if not readonly and str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK. Nested use joins the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def lint_migration(sql: str) -> None:
    """Migrations are additive only. Seed INSERTs and CREATE are fine; destructive statements are rejected."""
    stripped = re.sub(r"--[^\n]*", "", sql)
    m = FORBIDDEN_SQL.search(stripped)
    if m:
        raise MigrationError("destructive statement not allowed in migration: %r" % m.group(0))


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def migrate(conn: sqlite3.Connection, kind: str, directory: Path | None = None) -> list:
    """Apply pending migrations of `kind` ('service' or 'infra'). Verifies checksums of already-applied files."""
    directory = (directory or MIGRATIONS_DIR) / kind
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                 "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
    applied = {r["version"]: r["checksum"] for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
    done = []
    for path in sorted(directory.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        sql = path.read_text(encoding="utf-8")
        checksum = _checksum(sql)
        if version in applied:
            if applied[version] != checksum:
                raise MigrationError("migration %s was modified after being applied" % path.name)
            continue
        lint_migration(sql)
        try:
            conn.executescript("BEGIN IMMEDIATE;\n%s\nINSERT INTO schema_migrations VALUES(%d,'%s','%s','%s');\nCOMMIT;"
                               % (sql, version, path.name, checksum, timeutil.iso(timeutil.utcnow())))
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        done.append(path.name)
    return done


def open_service(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = path or (data_root() / "service_db" / "pitchboard_kg.sqlite3")
    conn = connect(path)
    migrate(conn, "service")
    return conn


def open_infra(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = path or (data_root() / "infra_db" / "infra.sqlite3")
    conn = connect(path)
    migrate(conn, "infra")
    return conn
