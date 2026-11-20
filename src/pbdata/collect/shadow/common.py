"""Strict configuration, safe credentials, and isolated file primitives."""
from __future__ import annotations

from contextlib import closing, contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from decimal import Decimal, DecimalException

UTC = dt.timezone.utc
CONFIG = Path(__file__).resolve().parents[4] / "config/llm_extract.json"
PROTECTED = tuple(Path(p) for p in (
    "/workspace/pitchboard", "/workspace/pitchboard-data", "/workspace/pitchboard-data-main",
    "/workspace/pb-eval"))
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
LEGACY_BUDGET_KEYS = {"daily_call_cap", "daily_cost_cap_usd", "monetary_cap"}
RATE_KEYS = ("input_usd_per_million_ceiling", "output_usd_per_million_ceiling")


class Stop(Exception):
    """Only fixed, non-sensitive reason codes may be passed to this exception."""


def digest(value):
    if not isinstance(value, bytes):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def source_content_hash(body):
    """The service collector's content hash for a raw response body.

    ``collect/net.py`` hashes ``body.decode("latin-1")`` through
    ``ids.sha256_hex`` (UTF-8 encoding), which differs from the raw-byte
    ``digest(body)`` for any non-ASCII page. Shadow target matching, cache
    lookup and history use this service contract while ``body_sha256`` remains
    the raw-byte identity used by held-out cohorts.
    """
    if not isinstance(body, bytes):
        raise Stop("invalid_policy_envelope")
    return digest(body.decode("latin-1"))


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def loads(value):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise Stop("duplicate_json_key")
            out[k] = v
        return out
    def constant(_):
        raise Stop("invalid_json_number")
    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def stamp(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError("UTC required")
    return parsed.astimezone(UTC)


def utcnow():
    return dt.datetime.now(UTC)


def money(value):
    if not isinstance(value, str):
        raise ValueError("decimal string required")
    result = Decimal(value)
    if not result.is_finite() or result < 0:
        raise ValueError("invalid decimal")
    return result


def pricing_at(pricing, now):
    """Return a validated rate snapshot usable at this UTC time."""
    try:
        verified = dt.date.fromisoformat(pricing["verified_on"])
        expiry = stamp(pricing["valid_until_utc"])
        if verified <= now.date() and now < expiry:
            return tuple(money(pricing[field]) for field in RATE_KEYS)
    except (AttributeError, DecimalException, KeyError, TypeError, ValueError):
        pass
    return None


def call_usage(provider, raw, rates):
    """Keep only numeric token counts, even when response validation fails."""
    usage = raw.get("usage") if isinstance(raw, dict) else None
    tokens = {k: usage[k] for k in TOKEN_FIELDS
              if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0}
    cost = None
    if (rates is not None and len(tokens) == len(TOKEN_FIELDS)
            and tokens["prompt_tokens"] + tokens["completion_tokens"] == tokens["total_tokens"]):
        try:
            cost = str((tokens["prompt_tokens"] * rates[0] + tokens["completion_tokens"] * rates[1])
                       / 1_000_000)
        except DecimalException:
            pass
    return {"provider": provider, "usage": tokens or None, "estimated_cost_usd": cost}


def total_cost(estimates):
    total = Decimal(0)
    try:
        for cost in estimates:
            if cost is None:
                return None
            total += money(cost)
    except DecimalException:
        return None
    return str(total)


def pricing_snapshot(pricing):
    """Keep only rate metadata in persisted config, independent of the clock."""
    try:
        dt.date.fromisoformat(pricing["verified_on"])
        stamp(pricing["valid_until_utc"])
        for field in RATE_KEYS:
            money(pricing[field])
        result = {k: pricing[k] for k in (*RATE_KEYS, "verified_on", "valid_until_utc")}
        token_ceiling = pricing.get("input_token_reservation_ceiling")
        if type(token_ceiling) is int and token_ceiling >= 0:
            result["input_token_reservation_ceiling"] = token_ceiling
        return result
    except (AttributeError, DecimalException, KeyError, TypeError, ValueError):
        return None


def load_config(path=CONFIG, now=None):
    """Fail closed. Disabled means no credentials, state, or network are touched."""
    try:
        c = loads(Path(path).read_bytes())
        if not isinstance(c, dict) or c.get("enabled") is not True:
            raise Stop("disabled")
        keys = {"enabled", "mode", "model", "egress",
                "max_page_utf8_bytes", "max_output_tokens", "timeout_seconds",
                "window_start_utc", "window_end_utc"}
        if (not keys <= set(c) <= keys | LEGACY_BUDGET_KEYS | {"pricing", "qwen_pricing"}
                or c["mode"] != "shadow" or c["model"] != "deepseek-flash"):
            raise Stop("invalid_config")
        if c["egress"] not in ("direct", "sandbox"):
            raise Stop("invalid_config")
        for k, low, high in (("max_page_utf8_bytes", 1, 24000),
                             ("max_output_tokens", 1, 2048), ("timeout_seconds", 1, 60)):
            if type(c[k]) is not int or not low <= c[k] <= high:
                raise Stop("invalid_config")
        start, end = stamp(c["window_start_utc"]), stamp(c["window_end_utc"])
        now = now or utcnow()
        if not start < end or end - start > dt.timedelta(days=7):
            raise Stop("invalid_window")
        if not start <= now < end:
            raise Stop("inactive_window")
        for field in ("pricing", "qwen_pricing"):
            if field in c:
                c[field] = pricing_snapshot(c[field])
        return {k: v for k, v in c.items() if k not in LEGACY_BUDGET_KEYS}
    except Stop:
        raise
    except FileNotFoundError:
        raise Stop("disabled") from None
    except Exception:
        raise Stop("invalid_config") from None


def page_limit(path=CONFIG):
    """Read the runner's page-size threshold without enabling extraction or touching state."""
    try:
        c = loads(Path(path).read_bytes())
        value = c.get("max_page_utf8_bytes") if isinstance(c, dict) else None
        if type(value) is not int or not 1 <= value <= 24000:
            raise Stop("invalid_config")
        return value
    except Stop:
        raise
    except FileNotFoundError:
        raise Stop("disabled") from None
    except Exception:
        raise Stop("invalid_config") from None


def load_key(env=None, path=None):
    env = os.environ if env is None else env
    if "DEEPSEEK_API_KEY" in env:
        value = env["DEEPSEEK_API_KEY"]
    else:
        path = Path(path) if path is not None else Path.home() / ".config/pitchboard/env"
        try:
            with secure_open(path, "r", create=False) as f:
                text = f.read(65537)
            if len(text) > 65536:
                raise Stop("invalid_credentials")
            lines = [line for line in text.splitlines() if re.match(r"^\s*(?:export\s+)?DEEPSEEK_API_KEY\b", line)]
            if len(lines) != 1:
                raise Stop("invalid_credentials")
            match = re.fullmatch(r"\s*(?:export )?DEEPSEEK_API_KEY=([A-Za-z0-9_.-]+|'[A-Za-z0-9_.-]+'|\"[A-Za-z0-9_.-]+\")\s*", lines[0])
            if match is None:
                raise Stop("invalid_credentials")
            value = match[1].strip("'\"")
        except Exception:
            raise Stop("missing_credentials") from None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", value):
        raise Stop("invalid_credentials")
    return value


QWEN_KEY_PATH = Path("/home/box/.qwen/token_plan_key")


def load_qwen_key(path=None):
    """Return the Qwen token-plan key, or "" if the file is absent.

    A missing key disables fallback. An existing but unsafe or malformed
    file raises, so a bad credential is never used. The value is never logged.
    """
    path = Path(path) if path is not None else QWEN_KEY_PATH
    try:
        path = no_symlinks(path)
        if not path.exists():
            return ""
        with secure_open(path, "r", create=False) as f:
            text = f.read(65537)
        if len(text) > 65536:
            raise Stop("invalid_credentials")
        value = text.strip()
    except Stop:
        raise
    except Exception:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", value):
        raise Stop("invalid_credentials")
    return value


def no_symlinks(path):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Stop("unsafe_path")
    return path


@contextmanager
def sqlite_snapshot(raw):
    """Query copied bytes without SQLite opening or updating the source DB."""
    if hasattr(sqlite3.Connection, "deserialize"):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.deserialize(raw)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            yield conn
    else:
        with tempfile.TemporaryDirectory(prefix="pb-shadow-snapshot-") as folder:
            with tempfile.NamedTemporaryFile(dir=folder) as snapshot:
                snapshot.write(raw)
                snapshot.flush()
                uri = Path(snapshot.name).as_uri() + "?mode=ro&immutable=1"
                with closing(sqlite3.connect(uri, uri=True)) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA query_only=ON")
                    yield conn


def secure_open(path, mode, create=True):
    path = no_symlinks(path)
    flags = os.O_NOFOLLOW | (os.O_RDONLY if mode == "r" else os.O_RDWR)
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid != os.getuid() or s.st_mode & 0o077:
        os.close(fd)
        raise Stop("unsafe_file")
    if mode == "w":
        os.ftruncate(fd, 0)
    return os.fdopen(fd, mode, encoding="utf-8")


def shadow_root(path, extra_protected=()):
    path = no_symlinks(path)
    real = path.resolve()
    for forbidden in (*PROTECTED, *extra_protected, Path(__file__).resolve().parents[4]):
        forbidden = Path(forbidden).resolve()
        if real == forbidden or real in forbidden.parents or forbidden in real.parents:
            raise Stop("protected_root")
    existed = path.exists()
    if existed and (not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise Stop("unsafe_root")
    if not existed:
        try:
            path.mkdir(mode=0o700, parents=False)
        except OSError:
            # A missing/unwritable parent is a fixed policy failure, not a traceback.
            raise Stop("unsafe_root") from None
    return path


def append_sync(f, value):
    f.seek(0, 2)
    f.write(dumps(value) + "\n")
    f.flush()
    os.fsync(f.fileno())


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
