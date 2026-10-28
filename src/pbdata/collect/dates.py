"""Rule-based date reading for en/ko notices. Only unambiguous dates with an explicit year are returned."""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import List, Optional

from ..timeutil import TZ_ABBR

MONTHS = {m: i for i, names in enumerate(
    [("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"), ("july", "jul"),
     ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"), ("december", "dec")], 1) for m in names}
MON_RE = "|".join(sorted(MONTHS, key=len, reverse=True))

PATTERNS = [
    # 2026-10-31 / 2026.10.31 / 2026. 10. 31. / 2026/10/31
    ("ymd", re.compile(r"(?<!\d)(20\d{2}|19\d{2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})(?!\d)")),
    # 2026년 10월 31일
    ("ymd_ko", re.compile(r"(20\d{2}|19\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")),
    # October 31, 2026 / Oct. 31st 2026
    ("mdy", re.compile(r"\b(%s)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2}|19\d{2})\b" % MON_RE, re.I)),
    # 31 October 2026
    ("dmy", re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(%s)\.?,?\s+(20\d{2}|19\d{2})\b" % MON_RE, re.I)),
]
# end-of-range pieces that inherit the start's year: "~ 10. 31." / "~ 10월 31일" / "- November 15"
RANGE_TAIL_KO = re.compile(r"[~∼–\-]\s*(?:(\d{1,2})\s*[./월]\s*(\d{1,2})\s*(?:일|\.)?)")
# "2026. 9. 14.(월) 10:00 ~ 10. 16.(금)": the start may carry a weekday and a 24h time before the range separator
RANGE_TAIL = re.compile(r"^(?:[\s.)(월화수목금토일a-zA-Z가-힣]|(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)){0,14}?[~∼–\-]\s*(\d{1,2})\s*[./월]\s*(\d{1,2})\s*(?:일|\.)?")
# 24h clock ("18:00까지", "23:59:59 (KST)"); only read when no am/pm time is present
TIME24_RE = re.compile(r"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(?![\d:])")
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b(?:\s*\(?(%s)\)?)?" % "|".join(TZ_ABBR), re.I)
TZ_RE = re.compile(r"\b(%s)\b" % "|".join(TZ_ABBR))


@dataclasses.dataclass
class DateHit:
    iso: str
    start: int
    end: int
    year_explicit: bool = True
    time: Optional[str] = None
    tz: Optional[str] = None
    offset: Optional[float] = None


def _mk(y, m, d) -> Optional[str]:
    try:
        return dt.date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def find_dates(text: str) -> List[DateHit]:
    hits: List[DateHit] = []
    taken = []
    for name, rx in PATTERNS:
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in taken):
                continue
            g = m.groups()
            if name in ("ymd", "ymd_ko"):
                iso = _mk(g[0], g[1], g[2])
            elif name == "mdy":
                iso = _mk(g[2], MONTHS[g[0].lower()], g[1])
            else:
                iso = _mk(g[2], MONTHS[g[1].lower()], g[0])
            if iso:
                hits.append(DateHit(iso, m.start(), m.end()))
                taken.append((m.start(), m.end()))
    hits.sort(key=lambda h: h.start)
    # range tail without year ("2026.10.1 ~ 10.31")
    extra: List[DateHit] = []
    for h in hits:
        tail = RANGE_TAIL.match(text[h.end:h.end + 50])
        if tail and not any(a.start == h.end + tail.start(1) for a in hits):
            iso = _mk(h.iso[:4], tail.group(1), tail.group(2))
            if iso and iso >= h.iso:
                extra.append(DateHit(iso, h.end + tail.start(1), h.end + tail.end(), year_explicit=False))
    hits.extend(extra)
    hits.sort(key=lambda h: h.start)
    for i, h in enumerate(hits):
        window = text[h.end:h.end + 60]
        t = TIME_RE.search(window)
        if t is None:
            # a 24h time belongs to this date only if it comes before the next date / range separator
            nxt = hits[i + 1].start - h.end if i + 1 < len(hits) else len(window)
            stop = min([nxt] + [k for k in (window.find("~"), window.find("∼"), window.find("–")) if k >= 0])
            t24 = TIME24_RE.search(window[:max(0, stop)])
            if t24:
                h.time = "%02d:%02d" % (int(t24.group(1)), int(t24.group(2)))
        if t:
            hh, mm, ap = int(t.group(1)), int(t.group(2) or 0), t.group(3).lower().replace(".", "")
            if 1 <= hh <= 12:
                hh = hh % 12 + (12 if ap == "pm" else 0)
                h.time = "%02d:%02d" % (hh, mm)
            if t.group(4):
                h.tz = t.group(4).upper()
        if h.tz is None:
            z = TZ_RE.search(window)
            if z:
                h.tz = z.group(1)
        if h.tz in TZ_ABBR:
            h.offset = float(TZ_ABBR[h.tz])
    return hits
