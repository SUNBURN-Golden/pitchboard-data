"""Private bounded parsed-source snapshots, never HTML or service DB bodies."""
import dataclasses
import datetime as dt
import os
from pathlib import Path
import re
import uuid

from .common import Stop, digest, dumps, loads, secure_open, stamp, sync_dir
from .policy import canonical_url
from ..parse import Doc, Segment, PARSER_VERSION

FORMAT = "pb-shadow-text-1"
POLICY = "shadow-policy-1"
MAX_ENVELOPE = 131072


def directory(state):
    path = state.output("2000-01-01").parent / "source_text"
    if path.is_symlink():
        raise Stop("unsafe_path")
    path.mkdir(mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise Stop("unsafe_path")
    return path


def cache_key(url, body_hash):
    return digest(dumps([FORMAT, POLICY, PARSER_VERSION, canonical_url(url), body_hash]))


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

    def doc(self):
        if self.provenance != "shadow_cached":
            raise Stop("invalid_policy_envelope")
        return self.document


def save(state, page, doc, now, key):
    payload = {"schema": FORMAT, "policy": POLICY, "parser": PARSER_VERSION,
               "requested_url": page.requested_url, "final_url": page.final_url,
               "body_sha256": page.body_sha256, "text_sha256": digest(doc.text()),
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
    name = cache_key(page.requested_url, page.body_sha256)
    path = directory(state) / (name + ".json")
    if path.exists() or path.is_symlink():
        previous = load(state.root, page.requested_url, page.body_sha256, now)
        if previous is not None:
            prior = previous.doc()
            if (previous.final_url != page.final_url or prior.text() != doc.text() or prior.title != doc.title
                    or prior.lang != doc.lang or prior.meta != payload["doc"]["meta"] or prior.segments != doc.segments):
                raise Stop("corrupt_source_cache")
            return name
    atomic_json(path, {"payload": payload, "sha256": digest(data)})
    return name


def load(root, url, body_hash, now, limit=24000):
    """Pure private-file read: never creates state, resolves DNS or fetches."""
    if body_hash is None:
        return None
    if not isinstance(body_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", body_hash):
        raise Stop("invalid_source_hash")
    name = cache_key(url, body_hash)
    path = Path(root) / "data/shadow/source_text" / (name + ".json")
    if not path.exists() and not path.is_symlink():
        return None
    try:
        with secure_open(path, "r", create=False) as f:
            data = f.read(MAX_ENVELOPE + 1)
        if len(data.encode("utf-8")) > MAX_ENVELOPE:
            raise Stop("invalid_source_cache")
        envelope = loads(data)
        p = envelope["payload"]
        if envelope["sha256"] != digest(dumps(p)):
            raise Stop("corrupt_source_cache")
        if p["schema"] != FORMAT or p["policy"] != POLICY or p["parser"] != PARSER_VERSION:
            return None
        if p["requested_url"] != canonical_url(url) or p["body_sha256"] != body_hash or p["truncated"] is not False:
            raise Stop("invalid_source_cache")
        canonical_url(p["final_url"])
        if not p["redirect_chain"] or p["redirect_chain"][-1] != p["final_url"]:
            raise Stop("invalid_source_cache")
        for hop in p["redirect_chain"]:
            canonical_url(hop)
        observed = stamp(p["observed_at_utc"])
        if observed > now:
            raise Stop("invalid_source_cache")
        if now - observed >= dt.timedelta(days=30):
            return None
        d = p["doc"]
        if set(d) != {"kind", "title", "lang", "meta", "segments"} or d["kind"] != "html" or not isinstance(d["segments"], list) or len(d["segments"]) > 2048:
            raise Stop("invalid_source_cache")
        if not all(isinstance(d[k], str) for k in ("title", "lang")) or not isinstance(d["meta"], dict) or set(d["meta"]) - {"og:title"} or not all(isinstance(v, str) for v in d["meta"].values()):
            raise Stop("invalid_source_cache")
        segments = [Segment(**s) for s in d["segments"]]
        for i, s in enumerate(segments):
            if type(s.idx) is not int or s.idx != i or not all(isinstance(v, str) for v in (s.kind, s.text, s.heading)) or not isinstance(s.locator, dict):
                raise Stop("invalid_source_cache")
        doc = Doc(d["kind"], title=d["title"], lang=d["lang"], meta=d["meta"], segments=segments)
        if digest(doc.text()) != p["text_sha256"] or not doc.text().strip():
            raise Stop("corrupt_source_cache")
        if len(doc.text().encode("utf-8")) > min(limit, 24000):
            raise Stop("page_too_large")
        return SourceText(p["requested_url"], p["final_url"], body_hash, tuple(p["redirect_chain"]), doc, p["observed_at_utc"])
    except Stop:
        raise
    except Exception:
        raise Stop("invalid_source_cache") from None
