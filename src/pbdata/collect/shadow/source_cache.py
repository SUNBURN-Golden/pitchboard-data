"""Private bounded parsed-source snapshots, never HTML or service DB bodies."""
import dataclasses
import datetime as dt
import os
from pathlib import Path
import re
import uuid

from .common import Stop, digest, dumps, loads, secure_open, source_content_hash, stamp, sync_dir
from .policy import canonical_url
from ..parse import Doc, Segment, PARSER_VERSION

FORMAT = "pb-shadow-text-2"
POLICY = "shadow-policy-1"
MAX_ENVELOPE = 131072
_HEX_RE = re.compile(r"[a-f0-9]{64}")
PAYLOAD_KEYS = {"schema", "policy", "parser", "requested_url", "final_url",
                "body_sha256", "source_content_hash", "text_sha256", "redirect_chain",
                "observed_at_utc", "original_provenance", "truncated", "doc"}


def directory(state):
    path = state.output("2000-01-01").parent / "source_text"
    if path.is_symlink():
        raise Stop("unsafe_path")
    path.mkdir(mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise Stop("unsafe_path")
    return path


def base_key(url, source_hash):
    """Stable discovery key for the requested URL and service content hash."""
    return digest(dumps([FORMAT, POLICY, PARSER_VERSION, canonical_url(url), source_hash]))


def entry_name(url, source_hash, final_url, text_sha256):
    """Full snapshot identity: requested/final URL, hashes and parser versions.

    Including ``final_url`` and the parsed-text hash means an identical body that
    resolves to a different final URL is a distinct snapshot rather than a
    ``corrupt_source_cache`` collision (F4).
    """
    suffix = digest(dumps([canonical_url(final_url), text_sha256]))[:32]
    return base_key(url, source_hash) + "-" + suffix


def candidate_paths(root, url, source_hash):
    """Existing snapshot paths for a requested URL + service hash; never creates."""
    root = Path(root)
    path = root / "data/shadow/source_text"
    if path.is_symlink():
        raise Stop("unsafe_path")
    if not path.is_dir():
        return []
    return sorted(p for p in path.glob(base_key(url, source_hash) + "-*.json"))


def atomic_json(path, value):
    temporary = path.with_name("." + uuid.uuid4().hex + ".tmp")
    try:
        # Validate existing destinations before replace, including hardlinks.
        if path.exists() or path.is_symlink():
            with secure_open(path, "r", create=False):
                pass
        with secure_open(temporary, "w") as f:
            f.write(dumps(value) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclasses.dataclass
class SourceText:
    requested_url: str
    final_url: str
    body_sha256: str
    hops: tuple
    document: Doc = dataclasses.field(repr=False)
    observed_at_utc: str = ""
    provenance: str = "shadow_cached"
    source_content_hash: str = ""
    cache_key: str = ""

    def doc(self):
        if self.provenance != "shadow_cached":
            raise Stop("invalid_policy_envelope")
        return self.document


def _document(d):
    if set(d) != {"kind", "title", "lang", "meta", "segments"} or d["kind"] != "html" or not isinstance(d["segments"], list) or len(d["segments"]) > 2048:
        raise Stop("invalid_source_cache")
    if not all(isinstance(d[k], str) for k in ("title", "lang")) or not isinstance(d["meta"], dict) or set(d["meta"]) - {"og:title"} or not all(isinstance(v, str) for v in d["meta"].values()):
        raise Stop("invalid_source_cache")
    try:
        segments = [Segment(**s) for s in d["segments"]]
    except (TypeError, ValueError):
        raise Stop("invalid_source_cache") from None
    for i, s in enumerate(segments):
        if type(s.idx) is not int or s.idx != i or not all(isinstance(v, str) for v in (s.kind, s.text, s.heading)) or not isinstance(s.locator, dict):
            raise Stop("invalid_source_cache")
    return Doc(d["kind"], title=d["title"], lang=d["lang"], meta=d["meta"], segments=segments)


def _decode(path, url, source_hash, now, limit):
    """Validate one snapshot. Returns the payload, None when unusable/expired."""
    with secure_open(path, "r", create=False) as f:
        data = f.read(MAX_ENVELOPE + 1)
    if len(data.encode("utf-8")) > MAX_ENVELOPE:
        raise Stop("invalid_source_cache")
    try:
        return _decode_loaded(data, url, source_hash, now, limit)
    except Stop:
        raise
    except Exception:
        raise Stop("invalid_source_cache") from None


def _decode_loaded(data, url, source_hash, now, limit):
    envelope = loads(data)
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "sha256"}:
        raise Stop("invalid_source_cache")
    p = envelope["payload"]
    if not isinstance(p, dict) or envelope["sha256"] != digest(dumps(p)):
        raise Stop("corrupt_source_cache")
    if set(p) != PAYLOAD_KEYS:
        raise Stop("invalid_source_cache")
    if p["schema"] != FORMAT or p["policy"] != POLICY or p["parser"] != PARSER_VERSION:
        return None
    if p["requested_url"] != canonical_url(url) or p["source_content_hash"] != source_hash or p["truncated"] is not False:
        raise Stop("invalid_source_cache")
    for key in ("body_sha256", "source_content_hash", "text_sha256"):
        if not isinstance(p[key], str) or not _HEX_RE.fullmatch(p[key]):
            raise Stop("invalid_source_cache")
    canonical_url(p["final_url"])
    if not isinstance(p["redirect_chain"], list) or not p["redirect_chain"] or p["redirect_chain"][-1] != p["final_url"]:
        raise Stop("invalid_source_cache")
    for hop in p["redirect_chain"]:
        canonical_url(hop)
    if p["original_provenance"] not in ("live_verified", "offline_cached"):
        raise Stop("invalid_source_cache")
    observed = stamp(p["observed_at_utc"])
    if observed > now:
        raise Stop("invalid_source_cache")
    if now - observed >= dt.timedelta(days=30):
        return None
    doc = _document(p["doc"])
    if digest(doc.text()) != p["text_sha256"] or not doc.text().strip():
        raise Stop("corrupt_source_cache")
    if len(doc.text().encode("utf-8")) > min(limit, 24000):
        raise Stop("page_too_large")
    return p


def save(state, page, doc, now, key):
    source_hash = page.source_content_hash
    text_hash = digest(doc.text())
    payload = {"schema": FORMAT, "policy": POLICY, "parser": PARSER_VERSION,
               "requested_url": page.requested_url, "final_url": page.final_url,
               "body_sha256": page.body_sha256, "source_content_hash": source_hash,
               "text_sha256": text_hash,
               "redirect_chain": list(page.hops), "observed_at_utc": getattr(page, "observed_at_utc", "") or now.isoformat(),
               "original_provenance": page.provenance, "truncated": False,
               "doc": {"kind": doc.kind, "title": doc.title, "lang": doc.lang,
                       "meta": {k: v for k, v in doc.meta.items() if k == "og:title"},
                       "segments": [dataclasses.asdict(s) for s in doc.segments]}}
    data = dumps(payload)
    if key in data:
        raise Stop("credential_in_page")
    if len(doc.text().encode("utf-8")) > 24000 or len(doc.segments) > 2048 or len(data.encode("utf-8")) > MAX_ENVELOPE - 128:
        raise Stop("page_too_large")
    name = entry_name(page.requested_url, source_hash, page.final_url, text_hash)
    path = directory(state) / (name + ".json")
    if path.exists() or path.is_symlink():
        previous = _decode(path, page.requested_url, source_hash, now, 24000)
        if previous is not None:
            prior = _document(previous["doc"])
            if (previous["body_sha256"] != page.body_sha256 or previous["final_url"] != canonical_url(page.final_url)
                    or previous["text_sha256"] != text_hash
                    or prior.text() != doc.text() or prior.title != doc.title
                    or prior.lang != doc.lang or prior.meta != payload["doc"]["meta"] or prior.segments != doc.segments):
                raise Stop("corrupt_source_cache")
            return name
        # An expired/incompatible entry at the same full identity is replaceable.
    atomic_json(path, {"payload": payload, "sha256": digest(data)})
    return name


def load(root, url, source_hash, now, limit=24000, *, final_url=None, text_sha256=None):
    """Pure private-file read: never creates state, resolves DNS or fetches."""
    if source_hash is None:
        return None
    if not isinstance(source_hash, str) or not _HEX_RE.fullmatch(source_hash):
        raise Stop("invalid_source_hash")
    chosen = []
    for path in candidate_paths(root, url, source_hash):
        payload = _decode(path, url, source_hash, now, limit)
        if payload is None:
            continue
        if final_url is not None and canonical_url(payload["final_url"]) != canonical_url(final_url):
            continue
        if text_sha256 is not None and payload["text_sha256"] != text_sha256:
            continue
        chosen.append((path.stem, payload))
    if not chosen:
        return None
    name, payload = max(chosen, key=lambda item: (stamp(item[1]["observed_at_utc"]), item[0]))
    return SourceText(payload["requested_url"], payload["final_url"], payload["body_sha256"],
                      tuple(payload["redirect_chain"]), _document(payload["doc"]),
                      payload["observed_at_utc"], source_content_hash=payload["source_content_hash"],
                      cache_key=name)
