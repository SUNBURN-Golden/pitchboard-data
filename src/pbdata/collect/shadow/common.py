"""Strict configuration, safe credentials, and isolated file primitives."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from decimal import Decimal

UTC = dt.timezone.utc
CONFIG = Path(__file__).resolve().parents[4] / "config/llm_extract.json"
PROTECTED = tuple(Path(p) for p in (
    "/workspace/pitchboard", "/workspace/pitchboard-data", "/workspace/pitchboard-data-main",
    "/workspace/pb-eval"))


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


def load_config(path=CONFIG, now=None):
    """Fail closed. Disabled means no credentials, state, or network are touched."""
    try:
        c = loads(Path(path).read_bytes())
        if not isinstance(c, dict) or c.get("enabled") is not True:
            raise Stop("disabled")
        keys = {"enabled", "mode", "model", "daily_call_cap", "daily_cost_cap_usd", "monetary_cap", "egress",
                "max_page_utf8_bytes", "max_output_tokens", "timeout_seconds",
                "window_start_utc", "window_end_utc", "pricing"}
        if set(c) != keys or c["mode"] != "shadow" or c["model"] != "deepseek-flash":
            raise Stop("invalid_config")
        if c["monetary_cap"] not in ("enforced", "none") or c["egress"] not in ("direct", "sandbox"):
            raise Stop("invalid_config")
        for k, low, high in (("daily_call_cap", 0, 10000), ("max_page_utf8_bytes", 1, 24000),
                             ("max_output_tokens", 1, 2048), ("timeout_seconds", 1, 60)):
            if type(c[k]) is not int or not low <= c[k] <= high:
                raise Stop("invalid_config")
        p = c["pricing"]
        if not isinstance(p, dict) or set(p) != {"input_usd_per_million_ceiling",
                "output_usd_per_million_ceiling", "input_token_reservation_ceiling", "verified_on", "valid_until_utc"}:
            raise Stop("invalid_config")
        if type(p["input_token_reservation_ceiling"]) is not int or p["input_token_reservation_ceiling"] != 1000000:
            raise Stop("invalid_config")
        if money(p["input_usd_per_million_ceiling"]) < Decimal("0.30") or money(p["output_usd_per_million_ceiling"]) < Decimal("1.20"):
            raise Stop("invalid_config")
        if c["daily_call_cap"] == 0:
            raise Stop("budget_exhausted")
        if c["monetary_cap"] == "enforced":
            if money(c["daily_cost_cap_usd"]) == 0:
                raise Stop("budget_exhausted")
        elif c["daily_cost_cap_usd"] is not None:
            # "none" is the only opt-in, and it cannot sit beside a dollar figure that looks enforced.
            raise Stop("invalid_config")
        start, end = stamp(c["window_start_utc"]), stamp(c["window_end_utc"])
        now = now or utcnow()
        if not start < end or end - start > dt.timedelta(days=7):
            raise Stop("invalid_window")
        if not start <= now < end:
            raise Stop("inactive_window")
        verified = dt.date.fromisoformat(p["verified_on"])
        expiry = stamp(p["valid_until_utc"])
        if verified > now.date() or expiry - dt.datetime.combine(verified, dt.time(), UTC) > dt.timedelta(days=7):
            raise Stop("invalid_pricing")
        if expiry < end or now >= expiry:
            raise Stop("expired_pricing")
        return c
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
