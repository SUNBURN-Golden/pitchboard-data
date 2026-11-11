"""Deterministic owner/outlet naming rules (no AI, no network).

A canonical name must be a name: UI/CTA labels ("온라인 접수하기", "Apply now"), bare Wikidata QIDs
("Q121252827"), and empty or URL-only strings are rejected. Rejected names fall back to the better
available candidates (Wikidata label, og:site_name, <title> brand, host-derived); nothing is invented.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Optional
from urllib.parse import urlparse

from .net import registered_domain

QID_RE = re.compile(r"Q\d+", re.I)
_HOST_PATH_RE = re.compile(r"^[\w-]+(?:\.[\w-]+)+/")          # host/path with no scheme and no spaces
_TITLE_SEP = re.compile(r"\s*[|–—·•:]\s*|\s+-\s+")

# UI / CTA / nav labels, matched after normalisation (NFKC + casefold + whitespace collapse + edge decoration strip).
CTA_LABELS = {
    # Korean
    "접수", "접수하기", "신청", "신청하기", "지원", "지원하기", "응모", "응모하기", "등록하기",
    "온라인 접수", "온라인 접수하기", "온라인 신청", "온라인 신청하기", "지금 접수", "지금 신청", "지금 신청하기",
    "바로가기", "바로 가기", "홈페이지 바로가기", "사이트 바로가기", "보러 가기", "보러가기",
    "더보기", "더 보기", "자세히 보기", "자세히보기", "상세 보기", "홈", "홈페이지", "메뉴", "목록", "로그인", "회원가입", "다음", "이전",
    # English
    "apply", "apply now", "apply online", "apply here", "submit", "submit now", "submit here", "submit online",
    "click here", "read more", "learn more", "see more", "view more", "more", "more info", "more information",
    "details", "view details", "see details", "sign up", "signup", "register", "register now", "enter", "enter now",
    "get started", "download", "home", "menu", "login", "log in", "sign in", "next", "previous", "back", "list",
}
# compounds the exact set cannot enumerate: "공모전 접수하기", "홈페이지 바로가기", ...
KO_CTA_RE = re.compile(r"(?:접수|신청|지원|응모|등록)\s*하기$|바로\s*가기$|더\s*보기$|자세히\s*보기$|보러\s*가기$")
EN_CTA_RE = re.compile(r"^click here\b|^(?:apply|submit)\s+(?:your|the|a)\b|^(?:read|learn|see|view)\s+more\b")


def _cta_key(s: str) -> str:
    k = " ".join(unicodedata.normalize("NFKC", s).casefold().split())
    return k.strip(" \t!?.…~→›»·•*-")


def is_junk_name(name: Optional[str]) -> bool:
    """True for empty/whitespace, bare Wikidata QIDs, URL-only strings and UI/CTA labels."""
    s = " ".join(unicodedata.normalize("NFKC", name or "").split())
    if not s or QID_RE.fullmatch(s):
        return True
    if "://" in s or s[:4].lower() == "www." or _HOST_PATH_RE.match(s):
        return True
    k = _cta_key(s)
    return not k or k in CTA_LABELS or bool(KO_CTA_RE.search(k)) or bool(EN_CTA_RE.search(k))


def clean_name(name: Optional[str]) -> Optional[str]:
    """Whitespace-normalised name, or None when the string is not a name (see is_junk_name)."""
    s = " ".join(unicodedata.normalize("NFKC", name or "").split())
    return None if not s or is_junk_name(s) else s


def host_name(host: str) -> str:
    """Last-resort name taken literally from the host (registrable domain, no www/port). Never invented."""
    h = (host or "").strip().lower()
    if "://" in h:
        h = urlparse(h).netloc or h
    h = h.split("/")[0].split(":")[0].removeprefix("www.")
    return registered_domain(h) if h else ""


def title_brand(title: str, host: str = "") -> Optional[str]:
    """Brand part of a page title: split on '|', '–', '·', ':', ' - '; prefer the part naming the host,
    else the shortest part (ties -> last, the common 'Page - Site' convention). CTA-only titles yield None."""
    parts = [clean_name(p) for p in _TITLE_SEP.split(" ".join((title or "").split()))]
    parts = [p for p in parts if p]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    label = host_name(host).split(".")[0]
    if label:
        for p in parts:
            if label in p.casefold():
                return p
    return min(reversed(parts), key=len)


def doc_names(doc, host: str = "") -> List[str]:
    """Name candidates read from an already-fetched page: site-name metas first, then the title brand."""
    out: List[str] = []
    for key in ("og:site_name", "application-name", "apple-mobile-web-app-title"):
        n = clean_name((doc.meta or {}).get(key, ""))
        if n and n not in out:
            out.append(n)
    n = title_brand(doc.title, host)
    if n and n not in out:
        out.append(n)
    return out


def pick_name(candidates: Iterable[Optional[str]], host: str = "") -> str:
    """First candidate that is a real name; otherwise the host-derived name. Deterministic, never invents."""
    for c in candidates:
        n = clean_name(c)
        if n:
            return n[:120]
    return host_name(host)[:120]
