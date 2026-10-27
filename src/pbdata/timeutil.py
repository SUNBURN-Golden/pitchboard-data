"""Time helpers. Storage is UTC ISO-8601 'YYYY-MM-DDTHH:MM:SSZ'. Display zone is Asia/Seoul (KST, UTC+9)."""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional, Tuple

UTC = dt.timezone.utc
KST = dt.timezone(dt.timedelta(hours=9))
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Time zones that may appear in guideline text (UTC offsets in hours).
TZ_ABBR = {
    "UTC": 0, "GMT": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7, "KST": 9, "JST": 9, "AOE": -12,
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetime")
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> dt.datetime:
    if not isinstance(text, str) or not ISO_RE.match(text):
        raise ValueError("not UTC ISO-8601: %r" % (text,))
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def epoch_to_iso(epoch: float) -> str:
    return iso(dt.datetime.fromtimestamp(float(epoch), UTC))


def iso_to_epoch(text: str) -> int:
    return int(parse_iso(text).timestamp())


def parse_date(text: str) -> dt.date:
    if not isinstance(text, str) or not DATE_RE.match(text):
        raise ValueError("not YYYY-MM-DD: %r" % (text,))
    return dt.date.fromisoformat(text)


def to_kst(text: str) -> str:
    return parse_iso(text).astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def deadline_interval(closes_on: str, tz_source: str, utc_offset_hours: Optional[float] = None,
                      closes_time: Optional[str] = None) -> Tuple[dt.datetime, dt.datetime]:
    """Earliest and latest absolute instant at which a date-only deadline could end.

    explicit tz: both equal. unknown tz: end of that day at UTC+14 (earliest) .. end of that day at UTC-12 (latest).
    """
    day = parse_date(closes_on)
    hh, mm, ss = 23, 59, 59
    if closes_time:
        hh, mm = [int(x) for x in closes_time.split(":")[:2]]
        ss = 0
    local = dt.datetime(day.year, day.month, day.day, hh, mm, ss)
    if tz_source == "explicit":
        if utc_offset_hours is None:
            raise ValueError("explicit tz needs offset")
        instant = (local - dt.timedelta(hours=utc_offset_hours)).replace(tzinfo=UTC)
        return instant, instant
    earliest = (local - dt.timedelta(hours=14)).replace(tzinfo=UTC)
    latest = (local + dt.timedelta(hours=12)).replace(tzinfo=UTC)
    return earliest, latest
