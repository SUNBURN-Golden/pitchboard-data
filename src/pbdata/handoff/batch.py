"""Batch files shared between the Qwen bot and the handoff daemon."""
from contextlib import closing
import hashlib
import json
import re
import secrets
import shutil
import sqlite3

from pbdata import timeutil

from .status import json_line, sync_dir, write_file

SCHEMA = "pb-qwen-batch-1"
BATCH_ID = re.compile(r"qb[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")
OUTCOMES = ("ok", "blocked", "robots_disallowed", "fetch_error", "llm_error", "skipped_cap")
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
CAP_FIELDS = {"max_pages_per_run", "max_llm_calls_per_run", "max_page_utf8_bytes", "timeout_seconds", "max_output_tokens"}


def service_path(root):
    return root / "service_db/pitchboard_kg.sqlite3"


def readonly(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def copy_database(source, target):
    with closing(readonly(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")


def write_batch(inbox, items, created_at, git_commit, caps, llm_calls, tokens):
    created_at = created_at.astimezone(timeutil.UTC)
    batch_id = "qb" + created_at.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    data = b"".join(json_line(item) for item in items)
    manifest = dict(schema=SCHEMA, batch_id=batch_id, created_at=timeutil.iso(created_at), git_commit=git_commit,
                    counts={outcome: sum(item["outcome"] == outcome for item in items) for outcome in OUTCOMES},
                    repair_attempted=sum(item.get("repair_attempted", False) for item in items),
                    repair_ok=sum(item.get("repair_ok", False) for item in items),
                    items_sha256=hashlib.sha256(data).hexdigest(), llm_calls=llm_calls, caps=caps, **tokens)
    staging = inbox / (".tmp-" + batch_id)
    staging.mkdir(parents=True)
    try:
        write_file(staging / "items.jsonl", data)
        write_file(staging / "manifest.json", json_line(manifest))
        sync_dir(staging)
        staging.rename(inbox / batch_id)
        sync_dir(inbox)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return batch_id


def ready_batches(inbox):
    """Yield only complete batches with a matching digest, oldest first."""
    if not inbox.is_dir():
        return
    for path in sorted(inbox.iterdir()):
        if not BATCH_ID.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
            continue
        manifest_file, items_file = path / "manifest.json", path / "items.jsonl"
        if any(file.is_symlink() or not file.is_file() for file in (manifest_file, items_file)):
            continue
        try:
            manifest = json.loads(manifest_file.read_bytes())
            if manifest["schema"] != SCHEMA or manifest["batch_id"] != path.name:
                continue
            created = timeutil.parse_iso(manifest["created_at"])
            if path.name[2:18] != created.strftime("%Y%m%dT%H%M%SZ"):
                continue
            counts, caps = manifest["counts"], manifest["caps"]
            if (not isinstance(counts, dict) or set(counts) != set(OUTCOMES)
                    or any(type(value) is not int or value < 0 for value in counts.values())
                    or not valid_caps(caps) or sum(counts.values()) > caps["max_pages_per_run"]
                    or not all(type(manifest[key]) is int and manifest[key] >= 0 for key in (*TOKEN_FIELDS, "llm_calls"))
                    or manifest["llm_calls"] > caps["max_llm_calls_per_run"]
                    or not re.fullmatch(r"[0-9a-f]{40}|unknown", manifest["git_commit"])):
                continue
            data = items_file.read_bytes()
            if hashlib.sha256(data).hexdigest() == manifest["items_sha256"]:
                yield path, data
        except (OSError, KeyError, ValueError, TypeError):
            continue


def valid_caps(caps):
    return (isinstance(caps, dict) and set(caps) == CAP_FIELDS
            and all(type(value) is int and value > 0 for value in caps.values()))
