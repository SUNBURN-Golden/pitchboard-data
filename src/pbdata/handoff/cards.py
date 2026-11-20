"""Generate local SVGs, PNGs, and draft copy for the weekly share cards."""

import argparse
from contextlib import nullcontext
import datetime as dt
from decimal import Decimal
import fcntl
import hashlib
import logging
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import tempfile
from urllib.parse import urlencode
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from pbdata import db, timeutil

from . import card_layout, site, status

LOG = logging.getLogger(__name__)
DEFAULT_OUT_ROOT = Path("/workspace/pitchboard/social/weekly-cards")
DEFAULT_PYTHON = "/workspace/pitchboard/venvs/cards/bin/python"
EASTERN = ZoneInfo("America/New_York")
KINDS = ("top10", "deadlines")
CHANNELS = ("x", "threads", "reddit")
LAYOUTS = {"x": (1200, 675), "portrait": (1080, 1350)}
ROW_LIMITS = {"top10": {"x": 10, "portrait": 10}, "deadlines": {"x": 8, "portrait": 10}}
TEMPLATES = {
    "top10": {"x": card_layout.top10_x_svg, "portrait": card_layout.top10_portrait_svg},
    "deadlines": {"x": card_layout.deadlines_x_svg, "portrait": card_layout.deadlines_portrait_svg},
}
PLACEHOLDERS = ("Outlet Name", "$000", "Nov 00", "Placeholder", "placeholder", "[OWNER")
PAY_CODE = rf"(?:{card_layout.CURRENCY_CODE}|NZD|HKD|SGD|JPY|INR|KRW|CHF|ZAR|MXN)"
PAY_SYMBOLS = "$£€¥₹₩"
PAY_CURRENCY = rf"(?:US|CA|AU|NZ|HK|SG|A|C|S|{PAY_CODE})?\s*[{PAY_SYMBOLS}]|{PAY_CODE}"
PAY_AMOUNT = re.compile(
    rf"(?<![\w.,])(?:(?P<prefix>{PAY_CURRENCY})\s*)?(?P<first>{card_layout.NUMBER})"
    rf"(?:(?:\s*[-–—]\s*|\s+to\s+)(?:(?P<upper_prefix>{PAY_CURRENCY})\s*)?(?P<upper>{card_layout.NUMBER}))?"
    rf"(?!\d|[,.]\d)(?P<suffix>\s+{PAY_CODE}\b|¢|c(?=\s*(?:/|per\b)))?", re.I,
)
DIRECT_UNIT = re.compile(
    r"(?:^\s*\+?\s*/|\bper\s+)(?P<count>[\d,]+\s+)?"
    r"(?:(?:printed|published|book|single-book|long|fiction|nonfiction|accepted)\s+)?(?P<noun>[a-z]+)", re.I,
)
UNIT_NOUN = re.compile(r"\b(?:(review|article|essay|page|piece|poem|interview)s?|stor(?:y|ies))\b", re.I)
UNIT_LABEL = re.compile(r"\b(flat|stipend|honorarium|hourly)\b", re.I)
UNIT_PHRASE = re.compile(r"\bfor\s+(?:(?:a|an|the)\s+)?([a-z-]+(?:\s+(?!(?:and|or|for|to|on|in|with)\b)[a-z-]+)?)", re.I)
PAY_CLAUSE = re.compile(r"[;(]|[,.]\s")
PAY_LIST_SEPARATOR = re.compile(r"\s*(?:,\s*(?:and|or)?|and|or)\s*", re.I)
PER_WORD_UNIT = re.compile(r"per (?:[\d,]+ )?words?")
CALL_NOT_LIVE = {"closed", "cancelled", "superseded", "archived", "withdrawn", "expired", "upcoming"}
ELIGIBILITY_RULE = (
    "Open and pitch-first outlets qualify. Periodic-calls outlets need a live call with a deadline on or after "
    "the generation date. Closed, no-public-guidelines, sponsored, featured, and affiliate listings are excluded. "
    "A clean listed rate and a site pay_usd_max value are required."
)
RESTRICTED_WORDING = re.compile(r"\b(?:AI|LLM|ChatGPT|artificial intelligence|machine learning)\b", re.I)
CONTACT = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b\d{3}[-. ]\d{3}[-. ]\d{4}\b")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
COMPLIANCE_CHECKLIST = (
    "Confirm posting approval and that the soft-launch hold has been lifted.",
    "Check for sponsorships, affiliate links, or compensation. Confirm any disclosure is prominent.",
    "Confirm the ranking is qualified and the wording implies no endorsement.",
    "Check the card for outlet logos and verify that fonts and graphics are licensed.",
    "Compare the displayed pay, dates, and conversion note with the source and check the dated footer.",
    "Check the destination link and tracking parameters. Keep the full list in the Reddit body.",
    "Read today's subreddit rules on self-promotion, posting days, flair, and links. Check operator disclosure.",
    "Review each channel's caption for duplication and confirm engagement automation is off.",
    "Check the cards and captions for personal information and prohibited wording.",
    "Check the SVGs, PNGs, and captions for missing or sample content.",
)


class CardError(Exception):
    """A card could not be generated or validated."""


def scheduled_kinds(mode: str, date: dt.date) -> tuple[str, ...]:
    if mode == "auto":
        return {1: ("top10",), 3: ("deadlines",)}.get(date.weekday(), ())
    if mode == "both":
        return KINDS
    if mode == "none":
        return ()
    return (mode,)


def date_label(date: dt.date) -> str:
    return f"{date.day} {MONTHS[date.month - 1]} {date.year}"


def optional_date(value):
    try:
        return timeutil.parse_date(value)
    except ValueError:
        return None


def label_for_sponsorship(row, name, featured):
    if name in featured or row.get("featured"):
        return "Featured · Sponsored"
    if row.get("sponsored") or row.get("affiliate"):
        return "Sponsored"
    return ""


def listed_pay(value):
    if not value or value.strip().lower() in ("", "not recorded", "not stated", "unpublished"):
        return "Not recorded"
    # A directory note about a separate source can follow an unpriced honorarium.
    primary_text = re.split(r"\(NBCC\b", value, maxsplit=1, flags=re.I)[0]
    if re.search(r"\bhonorari(?:um|a)\b", primary_text, re.I) and not card_layout.AMOUNT.search(primary_text):
        return "Honorarium"
    return value


def pay_unit(after, before):
    direct = DIRECT_UNIT.search(after)
    if direct:
        noun = direct["noun"].lower()
        if direct["count"] and direct["count"].strip() != "1":
            unit = f"per {direct['count'].strip()} {noun}"
        else:
            known = UNIT_NOUN.fullmatch(noun)
            noun = (known[1] or "story").lower() if known else noun
            unit = "per " + ("word" if noun == "words" else noun)
        if len(unit) <= 18:
            return unit
    label = UNIT_LABEL.search(after)
    if label:
        return label[1].lower()
    noun = UNIT_NOUN.search(after) or UNIT_NOUN.search(before)
    if noun:
        return "per " + (noun[1] or "story").lower()
    phrase = UNIT_PHRASE.search(after)
    if phrase:
        words = phrase[1].lower().split()
        return next((unit for unit in ("for " + " ".join(words), "for " + words[0]) if len(unit) <= 18), "")
    return ""


def pay_units(pay, matches):
    units = [""] * len(matches)
    for i in range(len(matches) - 1, -1, -1):
        match = matches[i]
        has_next = i + 1 < len(matches)
        next_start = matches[i + 1].start() if has_next else len(pay)
        prev_end = matches[i - 1].end() if i else 0
        between = pay[match.end():next_start]
        after = PAY_CLAUSE.split(between, maxsplit=1)[0]
        prior_clauses = PAY_CLAUSE.split(pay[prev_end:match.start()])
        before = prior_clauses[-1] if not i or len(prior_clauses) > 1 else ""
        unit = pay_unit(after, before)
        if not unit and has_next and PAY_LIST_SEPARATOR.fullmatch(between):
            unit = units[i + 1]
        units[i] = unit
    return units


def clean_rate(pay, *, per_word=False):
    """Return the listed figure and unit, keeping each currency separate."""
    matches = [match for match in PAY_AMOUNT.finditer(pay or "")
               if match["prefix"] or match["upper_prefix"] or match["suffix"]]
    rates = {}
    for match, unit in zip(matches, pay_units(pay, matches)):
        prefix = (match["prefix"] or match["upper_prefix"] or "").strip()
        upper_prefix = (match["upper_prefix"] or prefix).strip()
        suffix = match["suffix"] or ""
        short, long = sorted((prefix.upper(), upper_prefix.upper()), key=len)
        if short != long and (short not in PAY_SYMBOLS or not long.endswith(short)):
            continue
        currency = (long + suffix).strip().upper()
        figures = rates.setdefault((currency, unit), {})
        for number, mark in ((match["first"], prefix), (match["upper"], upper_prefix)):
            if number:
                amount = Decimal(number.replace(",", ""))
                spacer = " " if mark.isalpha() else ""
                figures.setdefault(amount, f"{mark}{spacer}{amount:,f}{suffix}")
    if not rates:
        return None
    preferred = [key for key in rates if bool(PER_WORD_UNIT.fullmatch(key[1])) == per_word]
    candidates = preferred or list(rates)
    # Choose the largest stated upper amount within one currency, with no FX comparison.
    currency = candidates[0][0]
    key = max((key for key in candidates if key[0] == currency), key=lambda key: max(rates[key]))
    figures = rates[key]
    low, high = min(figures), max(figures)
    return (figures[low] if low == high else f"{figures[low]}–{figures[high]}", key[1])


def live_call_deadline(call, start):
    if call.get("status") in CALL_NOT_LIVE:
        return None
    opens_value = call.get("opens")
    opens = optional_date(opens_value)
    deadline = optional_date(call.get("deadline"))
    if deadline is not None and deadline >= start and (not opens_value or (opens is not None and opens <= start)):
        return deadline
    return None


def select_top10(outlets, featured, calls, date):
    live_outlets = {call["outlet"].casefold() for call in calls if live_call_deadline(call, date)}
    rows, excluded = [], []
    for outlet in outlets:
        name, state = outlet.get("name"), outlet.get("status")
        sort_value = outlet.get("pay_usd_max")
        if label_for_sponsorship(outlet, name, featured):
            reason = "sponsored_or_featured"
        elif state not in ("open", "pitch-first", "periodic-calls"):
            reason = state or "missing_status"
        elif state == "periodic-calls" and (name or "").casefold() not in live_outlets:
            reason = "no_live_call"
        else:
            rate = clean_rate(outlet.get("pays"), per_word=sort_value is None and outlet.get("pay_usd_per_word") is not None)
            if rate is not None and sort_value is not None:
                figure, unit = rate
                rows.append(dict(outlet_name=name, rate_as_listed=figure, unit=unit,
                                 pay_usd_max=sort_value, last_checked=outlet.get("last_checked")))
                continue
            reason = "no_clean_rate" if rate is None else "no_pay_usd_max"
        excluded.append(dict(outlet_name=name, reason=reason))
    # Matches the Highest pay section of the committed pitchboard-site/build.py.
    rows.sort(key=lambda row: (-row["pay_usd_max"], (row["outlet_name"] or "").lower()))
    rows = rows[:ROW_LIMITS["top10"]["portrait"]]
    for rank, row in enumerate(rows, 1):
        row["rank"] = f"{rank:02d}"
    return rows, excluded


def select_deadlines(calls, featured, start):
    end = start + dt.timedelta(days=7)
    eligible = []
    for call in calls:
        deadline = live_call_deadline(call, start)
        if deadline is None or deadline > end:
            continue
        eligible.append((deadline, call))
    eligible.sort(key=lambda entry: (entry[0], str(entry[1].get("id") or "")))
    rows = []
    for rank, (deadline, call) in enumerate(eligible, 1):
        pay = listed_pay(call.get("pay"))
        rows.append(dict(rank=f"{rank:02d}", call_title=call.get("title"), outlet_name=call.get("outlet"),
                         deadline=deadline.isoformat(), deadline_date=date_label(deadline), rate_as_listed=pay,
                         has_listed_amount=bool(card_layout.AMOUNT.search(pay)), last_checked=call.get("last_checked"),
                         sponsorship=label_for_sponsorship(call, call.get("outlet"), featured)))
    return rows


def source_rows(kind, site_data, date, deadline_start):
    sponsors = status.read_json(site_data / "sponsors.json", {})
    featured = {entry["outlet"] for entry in sponsors.get("featured", [])}
    if kind == "top10":
        outlets = status.read_json(site_data / "outlets.json", None)
        if outlets is None:
            raise CardError("site data is missing outlets.json")
        checked = (optional_date(row.get("last_checked")) for row in outlets)
        calls = status.read_json(site_data / "calls.json", {"calls": []})
        rows, excluded = select_top10(outlets, featured, calls["calls"], date)
        return rows, max(filter(None, checked), default=None), excluded
    data = status.read_json(site_data / "calls.json", None)
    if data is None:
        raise CardError("site data is missing calls.json")
    return select_deadlines(data["calls"], featured, deadline_start), optional_date(data.get("updated")), []


def source_link(kind, channel, date):
    path, query = ("outlets", {"sort": "pay"}) if kind == "top10" else ("calls", {})
    query.update(utm_source=channel, utm_medium="social", utm_campaign="pb_weekly",
                 utm_content=f"{date:%Y%m%d}_{kind}")
    return f"https://pitchboard.soulbound.co.kr/{path}/?{urlencode(query)}"


def reddit_row(kind, row):
    label = f' [{row["sponsorship"]}]' if row.get("sponsorship") else ""
    if kind == "deadlines":
        return f'{row["call_title"]} — {row["outlet_name"]}{label} · closes {row["deadline_date"]} · {row["rate_as_listed"]}'
    unit = row["unit"]
    unit_suffix = f" {unit}" if unit else ""
    return row["outlet_name"] + " — " + row["rate_as_listed"] + unit_suffix


def post_copy(kind, rows, date, deadline_start):
    dated = date_label(date)
    count = len(rows)
    if kind == "top10":
        x = (f"Top {count} listed rates for review and criticism outlets as of {dated}.\n\n"
             "Ranked by the top of each stated range in The Pitchboard directory. Check terms with the outlet.")
        threads = (f"This week’s listed rates for book-review and criticism outlets, as of {dated}.\n\n"
                   "The card ranks the top of each outlet’s stated range in the directory. "
                   "Confirm current terms on the outlet’s own page.")
        title = f"Listed rates for {count} review and criticism outlets as of {dated} (confirm with each outlet)"
        context = "The list is ordered by the upper end of the rates stated in the directory."
    else:
        start, end = date_label(deadline_start), date_label(deadline_start + dt.timedelta(days=7))
        x = (f"Criticism calls closing in the next 7 days, as listed on {dated}.\n\n"
             "Confirm deadlines and terms on the outlet’s own page.")
        threads = (f"Here are upcoming deadlines for review and criticism calls, as listed on {dated}.\n\n"
                   f"The window runs from {start} through {end} US Eastern. "
                   "Check the outlet’s page for current dates and payment terms.")
        title = f"Review and criticism calls closing in the next 7 days, as listed on {dated}"
        context = f"The dates run from {start} through {end} US Eastern, with the nearest deadline first."
    disclosure = ("Includes a labeled sponsored listing.\n\n"
                  if any(row.get("sponsorship") for row in rows) else "")
    links = {channel: source_link(kind, channel, date) for channel in CHANNELS}
    copy = {
        "x": f"{disclosure}{x}\n\nPosted by the operator of The Pitchboard, a free directory.\n\n{links['x']}\n",
        "threads": (f"{disclosure}{threads}\n\n"
                    f"I publish The Pitchboard, a free directory, and the Open Calls newsletter.\n\n{links['threads']}\n"),
        "reddit": (
            f"{title}\n\n{disclosure}I run The Pitchboard, a free directory of outlets that pay "
            "for book reviews and cultural criticism. The entries are included for reference.\n\n"
            f"Figures and dates are as listed on {dated}. {context} "
            "Check each outlet’s page before pitching.\n\n"
            + "\n".join(f"{i}. {reddit_row(kind, row)}" for i, row in enumerate(rows, 1))
            + f"\n\nSource in the directory — {links['reddit']}\n"
        ),
    }
    if len(copy["x"].rstrip()) - len(links["x"]) + 23 > 280:
        raise CardError("X caption exceeds 280 characters")
    if len(copy["threads"].rstrip()) > 500:
        raise CardError("Threads caption exceeds 500 characters")
    return copy


def guard_text(text, label):
    for marker in PLACEHOLDERS:
        if marker in text:
            raise CardError(f"{label} contains forbidden marker {marker!r}")
    if RESTRICTED_WORDING.search(text) or CONTACT.search(text):
        raise CardError(f"{label} contains restricted wording or contact information")


def check_rows(kind, rows):
    if not rows:
        raise CardError("no eligible rows")
    required = ("outlet_name", "rate_as_listed", "rank")
    if kind == "deadlines":
        required += ("call_title", "deadline_date")
    for row in rows:
        for slot in required:
            if not isinstance(row[slot], str) or not row[slot].strip():
                raise CardError(f"row {row['rank']} has an empty {slot} slot")


def check_svg(svg, layout):
    doc = ElementTree.fromstring(svg)
    text = ["".join(node.itertext()) for node in doc if node.tag.rsplit("}", 1)[-1] in ("text", "title")]
    if not text or any(not value.strip() for value in text):
        raise CardError(f"{layout} SVG has an empty text slot")
    guard_text(" ".join(text), f"{layout} SVG")


def render_png(svg_path, png_path, width, height):
    python = os.environ.get("PB_CARDS_PYTHON", DEFAULT_PYTHON)
    code = ("import cairosvg, sys\n"
            "cairosvg.svg2png(url=sys.argv[1], write_to=sys.argv[2], "
            "output_width=int(sys.argv[3]), output_height=int(sys.argv[4]))")
    try:
        subprocess.run([python, "-c", code, str(svg_path), str(png_path), str(width), str(height)],
                       check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").splitlines()
        message = detail[-1] if detail else f"exit status {exc.returncode}"
        raise CardError(f"PNG rendering failed using {python} ({message})") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CardError(f"PNG rendering failed using {python} ({exc})") from exc


def check_png(path, width, height):
    if not path.is_file():
        raise CardError(f"PNG renderer did not create {path.name}")
    with path.open("rb") as image:
        header = image.read(24)
    expected = b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)
    if header != expected:
        raise CardError(f"{path.name} is not a {width}×{height} PNG")


def _complete(out_dir, filenames, provenance):
    try:
        files = ((out_dir / filename).stat() for filename in filenames)
        if not all(stat.S_ISREG(file.st_mode) and file.st_size > 0 for file in files):
            return False
        meta = status.read_json(out_dir / filenames[-1], {})
        return isinstance(meta, dict) and all(meta.get(key) == value for key, value in provenance.items())
    except (FileNotFoundError, ValueError):
        return False


def _publish(stage, out_dir, filenames, force, provenance):
    # Serialize publication with other invocations, including the standalone CLI.
    lock = os.open(out_dir.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not force and _complete(out_dir, filenames, provenance):
            return
        if not out_dir.exists():
            stage.rename(out_dir)
            return
        # Metadata marks a complete set. Publish it last so a retry can replace
        # an interrupted set while preserving the other kind in the same folder.
        (out_dir / filenames[-1]).unlink(missing_ok=True)
        for filename in filenames:
            (stage / filename).replace(out_dir / filename)
    finally:
        os.close(lock)


def generate_card(kind, date, site_data, out_root=DEFAULT_OUT_ROOT, *, deadline_start=None, force=False,
                  renderer=render_png, site_commit=None):
    """Generate a validated local draft, or reuse an existing complete draft."""
    out_dir = out_root / date.isoformat()
    filenames = [f"{kind}-{layout}.{ext}" for layout in LAYOUTS for ext in ("svg", "png")]
    filenames += [f"{kind}-copy-{channel}.txt" for channel in CHANNELS]
    meta_name = f"{kind}-meta.json"
    filenames.append(meta_name)
    provenance = dict(site_commit=site_commit,
                      site_data_source="archived_commit_with_overlay" if site_commit else "explicit_override")
    if site_commit:
        # The overlay can change even when the selected Git commit stays fixed.
        inputs = [status.read_json(site_data / name, None) for name in ("outlets.json", "calls.json", "sponsors.json")]
        provenance["site_data_sha256"] = hashlib.sha256(status.json_line(inputs)).hexdigest()
    if not force and _complete(out_dir, filenames, provenance):
        return out_dir
    deadline_start = deadline_start or date
    rows, source_date, excluded = source_rows(kind, site_data, date, deadline_start)
    used = rows[:ROW_LIMITS[kind]["portrait"]]
    check_rows(kind, used)
    copy = post_copy(kind, used, date, deadline_start)
    for channel, caption in copy.items():
        guard_text(caption, f"{channel} copy")

    out_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{kind}-", dir=out_root) as tmp:
        stage = Path(tmp)
        for layout, (width, height) in LAYOUTS.items():
            visible = rows[:ROW_LIMITS[kind][layout]]
            more = len(rows) - len(visible)
            note = "Ranking uses the directory’s dated USD conversion." if kind == "top10" else ""
            if more:
                note = f"And {more} more on The Pitchboard"
            svg = TEMPLATES[kind][layout](visible, date_label(date), note)
            check_svg(svg, layout)
            svg_path, png_path = (stage / f"{kind}-{layout}.{ext}" for ext in ("svg", "png"))
            svg_path.write_text(svg, encoding="utf-8")
            renderer(svg_path, png_path, width, height)
            check_png(png_path, width, height)
        for channel, caption in copy.items():
            (stage / f"{kind}-copy-{channel}.txt").write_text(caption, encoding="utf-8")
        meta = dict(kind=kind, date=date.isoformat(), source_data_date=source_date.isoformat() if source_date else None,
                    rows=used, row_counts={layout: min(len(rows), limit) for layout, limit in ROW_LIMITS[kind].items()},
                    eligible_rows=len(rows), checks_passed=["required_slots", "placeholder_guard", "copy_limits", "png_size"],
                    compliance_checklist=COMPLIANCE_CHECKLIST, posted=False, **provenance)
        if kind == "deadlines":
            meta["deadline_window"] = dict(start=deadline_start.isoformat(), timezone=EASTERN.key,
                                           end=(deadline_start + dt.timedelta(days=7)).isoformat())
        else:
            meta.update(eligibility_rule=ELIGIBILITY_RULE, excluded=excluded)
        (stage / meta_name).write_bytes(status.json_line(meta))
        _publish(stage, out_dir, filenames, force, provenance)
    return out_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--card", choices=(*KINDS, "auto"), required=True)
    parser.add_argument("--date", type=timeutil.parse_date, required=True)
    parser.add_argument("--root", type=Path, default=db.data_root())
    parser.add_argument("--site-repo", type=Path, default=site.DEFAULT_REPO)
    parser.add_argument("--site-data", type=Path)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    kinds = scheduled_kinds(args.card, args.date)
    if not kinds:
        return 0
    snapshot = {}
    source = (nullcontext() if args.site_data else
              site.site_snapshot(args.root, args.site_repo, args.date, snapshot, data_only=True))
    try:
        with source as work:
            site_data = args.site_data or work / "data"
            for kind in kinds:
                out_dir = generate_card(kind, args.date, site_data, args.out_root, force=args.force,
                                        site_commit=snapshot.get("commit"))
                print(f"weekly card {kind} ready in {out_dir}")
    except Exception as exc:
        LOG.error("weekly card %s failed: %s", args.card, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
