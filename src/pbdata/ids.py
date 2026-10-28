"""Stable, sortable IDs: <prefix>_<26-char Crockford ULID>. Legacy IDs are kept as aliases, never reused."""
from __future__ import annotations

import hashlib
import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def ulid(now_ms: int | None = None, rand: bytes | None = None) -> str:
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rnd = os.urandom(10) if rand is None else rand
    if len(rnd) != 10:
        raise ValueError("rand must be 10 bytes")
    return _encode(ms, 10) + _encode(int.from_bytes(rnd, "big"), 16)


def new_id(prefix: str, now_ms: int | None = None, rand: bytes | None = None) -> str:
    if not prefix.isalpha() or not prefix.islower():
        raise ValueError("prefix must be lowercase letters")
    return "%s_%s" % (prefix, ulid(now_ms, rand))


def stable_id(prefix: str, *parts: str) -> str:
    """Deterministic ID for registry-like rows that must be idempotent on reload."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]
    return "%s_%s" % (prefix, digest)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
