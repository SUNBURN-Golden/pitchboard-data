"""Parsers: HTML -> segments/links/tables, sitemap, RSS/Atom, PDF (text+tables, OCR if scanned), image OCR."""
from __future__ import annotations

import dataclasses
import html as htmllib
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

PARSER_VERSION = "parse-1"
BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "article", "td", "th", "dd", "dt", "blockquote", "caption", "figcaption", "pre"}
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "head"}


@dataclasses.dataclass
class Segment:
    idx: int
    kind: str               # h | p | li | row | line
    text: str
    heading: str = ""
    locator: Dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Link:
    href: str
    text: str
    rel: str = ""


@dataclasses.dataclass
class Doc:
    kind: str
    title: str = ""
    lang: str = ""
    canonical: str = ""
    segments: List[Segment] = dataclasses.field(default_factory=list)
    links: List[Link] = dataclasses.field(default_factory=list)
    feeds: List[str] = dataclasses.field(default_factory=list)
    next_pages: List[str] = dataclasses.field(default_factory=list)
    meta: Dict[str, str] = dataclasses.field(default_factory=dict)
    jsonld: List[dict] = dataclasses.field(default_factory=list)
    page_count: Optional[int] = None
    ocr_used: bool = False
    ocr_conf: Optional[float] = None
    notes: List[str] = dataclasses.field(default_factory=list)

    def text(self) -> str:
        return "\n".join(s.text for s in self.segments)


class _HTML(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.doc = Doc("html")
        self.skip = 0
        self.buf: List[str] = []
        self.heading = ""
        self.cur_tag = "p"
        self.in_title = False
        self.title_buf: List[str] = []
        self.a_href: Optional[str] = None
        self.a_rel = ""
        self.a_buf: List[str] = []
        self.row: Optional[List[str]] = None
        self.cell: Optional[List[str]] = None
        self.ld: Optional[List[str]] = None
        self.in_ld = False

    def _flush(self, kind: Optional[str] = None):
        text = " ".join("".join(self.buf).split())
        self.buf = []
        if not text:
            return
        kind = kind or ("h" if self.cur_tag.startswith("h") and len(self.cur_tag) == 2 else "li" if self.cur_tag == "li" else "p")
        if kind == "h":
            self.heading = text[:160]
        self.doc.segments.append(Segment(len(self.doc.segments), kind, text, self.heading))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html" and a.get("lang"):
            self.doc.lang = a["lang"].lower()
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self.in_ld, self.ld = True, []
            return
        if tag in SKIP_TAGS and tag != "head":
            self.skip += 1
            return
        if tag == "head":
            return
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            n = (a.get("name") or a.get("property") or "").lower()
            if n and a.get("content"):
                self.doc.meta[n] = a["content"][:300]
        if tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href")
            if href:
                if rel == "canonical":
                    self.doc.canonical = urljoin(self.base, href)
                elif rel == "next":
                    self.doc.next_pages.append(urljoin(self.base, href))
                elif rel == "alternate" and any(t in (a.get("type") or "") for t in ("rss", "atom")):
                    self.doc.feeds.append(urljoin(self.base, href))
        if self.skip:
            return
        if tag == "a" and a.get("href"):
            self.a_href, self.a_rel, self.a_buf = a["href"], (a.get("rel") or ""), []
        if tag == "tr":
            self._flush()
            self.row = []
        if tag in ("td", "th"):
            self.cell = []
        if tag in BLOCK_TAGS and tag not in ("td", "th"):
            self._flush()
            self.cur_tag = tag
        if tag == "br":
            self.buf.append(" ")

    def handle_endtag(self, tag):
        if tag == "script" and self.in_ld:
            self.in_ld = False
            try:
                obj = json.loads("".join(self.ld or []))
                self.doc.jsonld.extend(obj if isinstance(obj, list) else [obj])
            except ValueError:
                pass
            return
        if tag in SKIP_TAGS and tag != "head":
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = False
            self.doc.title = " ".join("".join(self.title_buf).split())[:200]
        if tag == "a" and self.a_href is not None:
            text = " ".join("".join(self.a_buf).split())
            self.doc.links.append(Link(urljoin(self.base, self.a_href.strip()), text[:160], self.a_rel))
            self.a_href = None
        if tag in ("td", "th") and self.cell is not None:
            if self.row is not None:
                self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        if tag == "tr" and self.row is not None:
            cells = [c for c in self.row if c]
            if len(cells) >= 2:
                seg = Segment(len(self.doc.segments), "row", " | ".join(cells), self.heading, {"cells": cells})
                self.doc.segments.append(seg)
            self.row = None
        if tag in BLOCK_TAGS and tag not in ("td", "th"):
            self._flush()
            self.cur_tag = "p"

    def handle_data(self, data):
        if self.in_ld and self.ld is not None:
            self.ld.append(data)
            return
        if self.skip:
            return
        if self.in_title:
            self.title_buf.append(data)
        if self.a_href is not None:
            self.a_buf.append(data)
        if self.cell is not None:
            self.cell.append(data)
        else:
            self.buf.append(data)

    def close(self):
        super().close()
        self._flush()


def parse_html(body: bytes, base_url: str) -> Doc:
    text = body.decode("utf-8", "replace") if body else ""
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    if m and m.group(1).lower() not in (b"utf-8", b"utf8"):
        try:
            text = body.decode(m.group(1).decode(), "replace")
        except LookupError:
            pass
    p = _HTML(base_url)
    p.feed(text)
    p.close()
    d = p.doc
    # numbered pagination links: ?page=N / /page/N/
    for l in d.links:
        if "next" in l.rel.lower().split():
            d.next_pages.append(l.href)
        elif re.search(r"([?&]p(age)?=\d+|/page/\d+|/p/\d+)", l.href) and re.fullmatch(r"\d{1,3}|next( page)?|older( posts| calls)?|›|»|다음|more", l.text.strip().lower()):
            d.next_pages.append(l.href)
    d.next_pages = list(dict.fromkeys(d.next_pages))
    return d


# ---------------------------------------------------------------- sitemap / feeds
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(body: bytes) -> Tuple[List[str], List[str]]:
    """Return (page_urls, child_sitemaps). Refuses DTDs (entity expansion)."""
    if b"<!DOCTYPE" in body[:2000] or b"<!ENTITY" in body[:5000]:
        return [], []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return [], []
    pages, kids = [], []
    for el in root.iter():
        if _local(el.tag) == "loc" and el.text:
            (kids if _local(root.tag) == "sitemapindex" else pages).append(el.text.strip())
    return pages, kids


def parse_feed(body: bytes) -> List[Dict[str, str]]:
    if b"<!DOCTYPE" in body[:2000] or b"<!ENTITY" in body[:5000]:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    items = []
    for el in root.iter():
        if _local(el.tag) in ("item", "entry"):
            d = {"title": "", "link": "", "date": "", "summary": ""}
            for c in el:
                n = _local(c.tag)
                if n == "title":
                    d["title"] = (c.text or "").strip()
                elif n == "link":
                    d["link"] = (c.get("href") or c.text or "").strip()
                elif n in ("pubDate", "updated", "published", "date"):
                    d["date"] = (c.text or "").strip()
                elif n in ("description", "summary", "content"):
                    d["summary"] = re.sub(r"<[^>]+>", " ", htmllib.unescape(c.text or ""))[:300]
            items.append(d)
    return items


# ---------------------------------------------------------------- PDF / OCR
def _run(cmd: List[str], inp: Optional[bytes] = None, timeout: int = 120) -> bytes:
    return subprocess.run(cmd, input=inp, capture_output=True, timeout=timeout, check=True).stdout


def ocr_png(path: str, langs: str = "kor+eng") -> Tuple[str, float]:
    tsv = _run(["tesseract", path, "-", "-l", langs, "--psm", "6", "tsv"], timeout=180).decode("utf-8", "replace")
    words, confs, lines = [], [], {}
    for row in tsv.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            conf = float(c[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        confs.append(conf)
        lines.setdefault((c[2], c[3], c[4]), []).append(c[11])
    text = "\n".join(" ".join(v) for v in lines.values())
    return text, (sum(confs) / len(confs) if confs else 0.0)


def parse_image(body: bytes, langs: str = "kor+eng") -> Doc:
    d = Doc("image", ocr_used=True)
    if not shutil.which("tesseract"):
        d.notes.append("ocr_unavailable")
        return d
    with tempfile.TemporaryDirectory(prefix="pbimg") as tmp:
        path = os.path.join(tmp, "in.img")
        with open(path, "wb") as fh:
            fh.write(body)
        try:
            from PIL import Image   # normalise format + upscale small notices for OCR
            im = Image.open(path)
            im.load()
            if im.width < 1400:
                f = 1400 / im.width
                im = im.resize((1400, int(im.height * f)))
            png = os.path.join(tmp, "in.png")
            im.convert("L").save(png)
            path = png
        except Exception as e:  # PIL missing or unreadable image
            d.notes.append("image_decode_failed:%s" % type(e).__name__)
            return d
        text, conf = ocr_png(path, langs)
    d.ocr_conf = round(conf, 1)
    for i, line in enumerate([l for l in text.splitlines() if l.strip()]):
        d.segments.append(Segment(len(d.segments), "line", " ".join(line.split()), "", {"line": i}))
    return d


def parse_pdf(body: bytes, max_ocr_pages: int = 6, langs: str = "kor+eng") -> Doc:
    d = Doc("pdf")
    if not shutil.which("pdftotext"):
        d.notes.append("pdftotext_unavailable")
        return d
    with tempfile.TemporaryDirectory(prefix="pbpdf") as tmp:
        path = os.path.join(tmp, "in.pdf")
        with open(path, "wb") as fh:
            fh.write(body)
        try:
            raw = _run(["pdftotext", "-layout", "-enc", "UTF-8", path, "-"]).decode("utf-8", "replace")
        except subprocess.SubprocessError:
            d.notes.append("pdf_unreadable")
            return d
        pages = raw.split("\f")
        if pages and not pages[-1].strip():
            pages = pages[:-1]
        d.page_count = len(pages)
        chars = sum(len(p.strip()) for p in pages)
        has_images = False
        if pages and 0 < chars / max(1, len(pages)) < 80 and shutil.which("pdfimages"):
            try:
                has_images = len(_run(["pdfimages", "-list", path]).decode("utf-8", "replace").strip().splitlines()) > 2
            except subprocess.SubprocessError:
                has_images = True
        if pages and (chars == 0 or (chars / max(1, len(pages)) < 80 and has_images)):       # scanned: no usable text layer (short real text stays text)
            d.notes.append("scanned_pdf_ocr")
            d.ocr_used = True
            if shutil.which("pdftoppm") and shutil.which("tesseract"):
                _run(["pdftoppm", "-r", "200", "-l", str(max_ocr_pages), "-png", path, os.path.join(tmp, "pg")], timeout=180)
                pages, confs = [], []
                for f in sorted(x for x in os.listdir(tmp) if x.startswith("pg") and x.endswith(".png")):
                    t, c = ocr_png(os.path.join(tmp, f), langs)
                    pages.append(t)
                    confs.append(c)
                d.ocr_conf = round(sum(confs) / len(confs), 1) if confs else None
            else:
                d.notes.append("ocr_unavailable")
                pages = []
        for pno, page in enumerate(pages, 1):
            for lno, line in enumerate(page.splitlines()):
                if not line.strip():
                    continue
                cells = [c for c in re.split(r"\s{2,}", line.strip()) if c]
                if len(cells) >= 3 and not d.ocr_used:
                    d.segments.append(Segment(len(d.segments), "row", " | ".join(cells), "", {"page": pno, "line": lno, "cells": cells}))
                else:
                    d.segments.append(Segment(len(d.segments), "line", " ".join(line.split()), "", {"page": pno, "line": lno}))
    # title: first non-trivial line
    for s in d.segments:
        if len(s.text) > 6:
            d.title = s.text[:160]
            break
    return d


def merge_wrapped_lines(doc: Doc) -> Doc:
    """PDF/OCR text wraps mid-sentence; join consecutive 'line' segments into paragraphs (keeps rows)."""
    out: List[Segment] = []
    buf: List[Segment] = []

    def flush():
        if buf:
            out.append(Segment(len(out), "p", " ".join(b.text for b in buf), "", dict(buf[0].locator)))
            buf.clear()
    for s in doc.segments:
        if s.kind == "line":
            buf.append(s)
            if re.search(r"[.!?。다요함음]\s*$", s.text) or sum(len(b.text) for b in buf) > 220:
                flush()
        else:
            flush()
            out.append(Segment(len(out), s.kind, s.text, s.heading, s.locator))
    flush()
    doc.segments = out
    return doc
