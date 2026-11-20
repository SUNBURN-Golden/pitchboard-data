"""Fetch executor: API/feed -> HTML/structured -> browser render (dynamic only). Stores raw ref + hashes + extractor version in
layer 1, extraction rows in layer 2, labels in layer 3. Raw bodies are never persisted (decision 2)."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sqlite3
from typing import List, Optional
from urllib.parse import urlparse

from .. import timeutil
from ..db import transaction
from ..ids import sha256_hex
from ..profiles import pitchboard as prof
from . import parse, render, store
from .net import FetchResult, Fetcher, normalize_url, page_url

PDF_MAX = 15_000_000
IMG_MAX = 8_000_000
EXECUTOR_VERSION = "exec-1+" + prof.EXTRACTOR_VERSION + "+" + parse.PARSER_VERSION


@dataclasses.dataclass
class PageResult:
    url: str
    raw_id: Optional[str]
    outcome: str
    change_state: str
    kind: str
    doc: Optional[parse.Doc] = None
    exts: List[prof.Ext] = dataclasses.field(default_factory=list)
    labels: List[prof.LabelResult] = dataclasses.field(default_factory=list)
    requests: int = 0
    source_id: Optional[str] = None
    revision_id: Optional[str] = None
    reextracted: bool = False
    ocr: bool = False
    skip_reason: Optional[str] = None


def kind_for(url: str, ctype: str) -> str:
    u = url.lower().split("?")[0]
    if ctype == "application/pdf" or u.endswith(".pdf"):
        return "pdf"
    if ctype.startswith("image/") or u.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "image"
    if "xml" in ctype and ("rss" in ctype or "atom" in ctype) or u.endswith((".rss", "/feed", "/feed/")):
        return "feed"
    if u.endswith(".xml") and "sitemap" in u:
        return "sitemap"
    return "html"


def _accept(url: str) -> str:
    k = kind_for(url, "")
    return {"pdf": "application/pdf", "image": "image/*", "feed": "application/rss+xml,application/atom+xml,application/xml",
            "sitemap": "application/xml,text/xml"}.get(k, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")


def fetch_page(svc: sqlite3.Connection, fetcher: Fetcher, url: str, site_source_id: str, now: str, *, run_id: str = "",
               parent_raw_id: Optional[str] = None, depth: int = 0, force_reextract: bool = False, infra=None,
               prefetched: Optional[FetchResult] = None, refresh: bool = False) -> PageResult:
    url = normalize_url(url)
    if svc.execute("SELECT 1 FROM pb_source WHERE canonical_url=? AND retired_reason IS NOT NULL", (url,)).fetchone():
        return PageResult(url, None, "skipped", "", "", skip_reason="retired_source")
    if refresh:
        # Refresh the previously successful source, including historical image and share URLs.
        prev = store.last_raw(svc, url, active_source=True)
        if prev is None:
            return PageResult(url, None, "skipped", "", "", skip_reason="no_active_successful_source")
        page_source = prev["source_id"]
    else:
        clean_url = page_url(url)
        if clean_url is None:
            return PageResult(url, None, "skipped", "", "", skip_reason="binary_asset")
        url = clean_url
        page_source = store.ensure_page_source(svc, url, site_source_id, now)
        if page_source is None:
            return PageResult(url, None, "skipped", "", "", skip_reason="retired_source")
        prev = store.last_raw(svc, url)
    max_bytes = PDF_MAX if kind_for(url, "") == "pdf" else IMG_MAX if kind_for(url, "") == "image" else 4_000_000
    res: FetchResult = prefetched if prefetched is not None else fetcher.fetch(url, accept=_accept(url), max_bytes=max_bytes, now_iso=now)
    kind = kind_for(res.final_url, res.content_type)
    method = {"feed": "feed", "sitemap": "structured", "pdf": "pdf", "image": "ocr"}.get(kind, "html")
    doc: Optional[parse.Doc] = None
    if res.outcome == "needs_render":
        body = render.render_html(res.body, res.final_url)
        if body and len(body) > 200:
            d2 = parse.parse_html(body, res.final_url)
            if sum(len(s.text) for s in d2.segments) > 200:
                res.outcome, res.error_class, res.body, method = "ok", "", body, "browser"
        if res.outcome == "needs_render":
            res.error_class = "js_shell_render_" + ("unavailable" if render.binary() is None else "insufficient")
    base = dict(source_id=page_source, url=url, final_url=res.final_url, kind=kind if kind in ("html", "pdf", "image", "feed", "sitemap") else "other",
                fetch_method=method, content_type=res.content_type, bytes_len=len(res.body), content_hash=res.content_hash or None,
                http_status=res.status, outcome=res.outcome, error_class=res.error_class or None, extractor_version=EXECUTOR_VERSION,
                parent_raw_id=parent_raw_id, depth=depth, run_id=run_id, fetched_at=now, prev_raw_id=prev["raw_id"] if prev else None)
    if res.outcome != "ok":
        with transaction(svc):
            rid = store.insert_raw(svc, now, change_state="failed", **base)
        if infra is not None and res.outcome in ("login_wall", "challenge", "denied_robots", "denied_http"):
            store.alert(infra, res.outcome, url, {"status": res.status, "error": res.error_class}, rid, now)
        if infra is not None and res.outcome == "not_found" and prev:
            store.alert(infra, "page_gone", url, {"prev_raw": prev["raw_id"]}, rid, now)
        return PageResult(url, rid, res.outcome, "failed", base["kind"], requests=res.requests_used, source_id=page_source)
    ocr = False
    if kind == "pdf":
        doc = parse.merge_wrapped_lines(parse.parse_pdf(res.body))
        ocr = doc.ocr_used
    elif kind == "image":
        doc = parse.merge_wrapped_lines(parse.parse_image(res.body))
        ocr = True
    elif kind == "feed":
        items = parse.parse_feed(res.body)
        doc = parse.Doc("feed", title="feed")
        for i, it in enumerate(items):
            doc.segments.append(parse.Segment(i, "p", (it["title"] + " " + it["summary"]).strip(), "", {"link": it["link"], "date": it["date"]}))
            doc.links.append(parse.Link(it["link"], it["title"]))
    elif kind == "sitemap":
        pages, kids = parse.parse_sitemap(res.body)
        doc = parse.Doc("sitemap", title="sitemap")
        doc.links = [parse.Link(u, "") for u in pages] + [parse.Link(u, "sitemap") for u in kids]
        doc.segments = [parse.Segment(0, "p", "sitemap %d urls" % len(pages), "")]
    else:
        doc = parse.parse_html(res.body, res.final_url)
    seg_texts = [s.text for s in doc.segments]
    seg_hashes = [store.seg_hash(t) for t in seg_texts]
    text_hash = sha256_hex("\n".join(" ".join(t.split()) for t in seg_texts)) if seg_texts else None
    if not seg_texts and kind in ("html", "pdf", "image"):
        res.outcome, res.error_class = "empty_body", ("ocr_no_text" if ocr else "no_segments")
        with transaction(svc):
            base.update(outcome=res.outcome, error_class=res.error_class)
            rid = store.insert_raw(svc, now, change_state="failed", **base)
        return PageResult(url, rid, res.outcome, "failed", base["kind"], doc=doc, requests=res.requests_used, source_id=page_source, ocr=ocr)
    if prev is None:
        change = "new"
    elif prev["text_hash"] == text_hash:
        change = "unchanged"
    else:
        change = "changed"
    reextract = change == "unchanged" and (prev["extractor_version"] != EXECUTOR_VERSION or force_reextract)
    exts: List[prof.Ext] = []
    labels: List[prof.LabelResult] = []
    with transaction(svc):
        rid = store.insert_raw(svc, now, change_state=change, text_hash=text_hash, seg_hashes=json.dumps(seg_hashes[:2000]),
                               segment_count=len(seg_hashes), page_count=doc.page_count, **base)
        revision_id = store.ensure_revision(svc, page_source, text_hash or "", seg_hashes, now, EXECUTOR_VERSION)
        if change != "unchanged" or reextract:
            if kind in ("html", "pdf", "image"):
                exts = prof.extract(doc, ocr=ocr) + (prof.link_signals(doc) if kind == "html" else [])
                site = svc.execute("SELECT authority FROM pb_source WHERE source_id=?", (page_source,)).fetchone()
                labels = prof.label_doc(doc, exts, dt.date.fromisoformat(now[:10]), source_official=site["authority"] == "official_primary")
                store.put_extractions(svc, rid, exts, prof.OCR_EXTRACTOR if ocr else "rules", prof.EXTRACTOR_VERSION, now)
                store.put_labels(svc, rid, labels, prof.LABELER_VERSION, now)
    if infra is not None and prev is not None and change == "changed":
        _breakage_checks(svc, infra, prev, rid, url, exts, doc, now)
    return PageResult(url, rid, "ok", change, base["kind"], doc=doc, exts=exts, labels=labels, requests=res.requests_used,
                      source_id=page_source, revision_id=revision_id, reextracted=reextract, ocr=ocr)


CORE = ("deadline.closes_on", "fee.submission", "compensation.rate", "rights.simultaneous", "policy.ai", "eligibility.region", "status.declared")


def _breakage_checks(svc, infra, prev, rid, url, exts, doc, now) -> None:
    """Extraction-breakage detection: a page that used to yield core fields now yields none or lost some, or its
    structure collapsed, while the page is still fetchable. Alerts only; never auto-corrects facts."""
    old = {r["field"] for r in svc.execute("SELECT DISTINCT field FROM pb_extraction WHERE raw_id=?", (prev["raw_id"],)) if r["field"] in CORE}
    new = {e.field for e in exts if e.field in CORE}
    old_len, new_len = prev["segment_count"] or 0, len(doc.segments)
    if len(old) >= 2 and not new:
        store.alert(infra, "extraction_breakage", url, {"old_fields": sorted(old), "new_fields": [], "segments": [old_len, new_len]}, rid, now)
    elif old - new:
        store.alert(infra, "field_lost", url, {"lost": sorted(old - new), "kept": sorted(new & old)}, rid, now)
    if old_len >= 8 and new_len < old_len * 0.3:
        store.alert(infra, "segments_collapsed", url, {"segments": [old_len, new_len]}, rid, now)
