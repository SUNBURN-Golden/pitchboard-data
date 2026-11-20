"""Generate local SVGs, PNGs, and draft copy for the weekly share cards."""

import argparse
import datetime as dt
import fcntl
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

from pbdata import timeutil

from . import card_layout, status

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
UNIT = re.compile(r"\bper (?:review|word|article|printed page|page|essay|piece)\b", re.I)
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


def select_top10(outlets, featured):
    eligible = (row for row in outlets if row.get("pay_usd_max") is not None
                and not label_for_sponsorship(row, row.get("name"), featured))
    # pitchboard-site/build.py's Highest pay strip uses this key and includes closed outlets.
    ranked = sorted(eligible, key=lambda row: (-row["pay_usd_max"], (row.get("name") or "").lower()))
    rows = []
    for rank, outlet in enumerate(ranked[:ROW_LIMITS["top10"]["portrait"]], 1):
        pay = listed_pay(outlet.get("pays"))
        units = UNIT.findall(pay)
        unit = outlet.get("unit") or (units[0] if len(units) == 1 and len(card_layout.AMOUNT.findall(pay)) == 1 else "")
        rows.append(dict(rank=f"{rank:02d}", outlet_name=outlet.get("name"), rate_as_listed=pay, unit=unit,
                         pay_usd_max=outlet["pay_usd_max"], last_checked=outlet.get("last_checked")))
    return rows


def select_deadlines(calls, featured, start):
    end = start + dt.timedelta(days=7)
    eligible = []
    for call in calls:
        if call.get("status") in ("closed", "cancelled", "superseded", "archived"):
            continue
        deadline = optional_date(call.get("deadline"))
        opens_value = call.get("opens")
        opens = optional_date(opens_value)
        if deadline is None or not start <= deadline <= end or (opens_value and (opens is None or opens > start)):
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


def source_rows(kind, site_data, deadline_start):
    sponsors = status.read_json(site_data / "sponsors.json", {})
    featured = {entry["outlet"] for entry in sponsors.get("featured", [])}
    if kind == "top10":
        outlets = status.read_json(site_data / "outlets.json", None)
        if outlets is None:
            raise CardError("site data is missing outlets.json")
        checked = (optional_date(row.get("last_checked")) for row in outlets)
        return select_top10(outlets, featured), max(filter(None, checked), default=None)
    data = status.read_json(site_data / "calls.json", None)
    if data is None:
        raise CardError("site data is missing calls.json")
    return select_deadlines(data["calls"], featured, deadline_start), optional_date(data.get("updated"))


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
    unit_suffix = f" {unit}" if unit and unit not in row["rate_as_listed"] else ""
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


def _complete(out_dir, filenames):
    try:
        files = ((out_dir / filename).stat() for filename in filenames)
        return all(stat.S_ISREG(file.st_mode) and file.st_size > 0 for file in files)
    except FileNotFoundError:
        return False


def _publish(stage, out_dir, filenames, force):
    # Serialize publication with other invocations, including the standalone CLI.
    lock = os.open(out_dir.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not force and _complete(out_dir, filenames):
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
                  renderer=render_png):
    """Generate a validated local draft, or reuse an existing complete draft."""
    out_dir = out_root / date.isoformat()
    filenames = [f"{kind}-{layout}.{ext}" for layout in LAYOUTS for ext in ("svg", "png")]
    filenames += [f"{kind}-copy-{channel}.txt" for channel in CHANNELS]
    meta_name = f"{kind}-meta.json"
    filenames.append(meta_name)
    if not force and _complete(out_dir, filenames):
        return out_dir
    deadline_start = deadline_start or date
    rows, source_date = source_rows(kind, site_data, deadline_start)
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
                    compliance_checklist=COMPLIANCE_CHECKLIST, posted=False)
        if kind == "deadlines":
            meta["deadline_window"] = dict(start=deadline_start.isoformat(), timezone=EASTERN.key,
                                           end=(deadline_start + dt.timedelta(days=7)).isoformat())
        (stage / meta_name).write_bytes(status.json_line(meta))
        _publish(stage, out_dir, filenames, force)
    return out_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--card", choices=(*KINDS, "auto"), required=True)
    parser.add_argument("--date", type=timeutil.parse_date, required=True)
    parser.add_argument("--site-data", type=Path, required=True)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    for kind in scheduled_kinds(args.card, args.date):
        try:
            out_dir = generate_card(kind, args.date, args.site_data, args.out_root, force=args.force)
        except Exception as exc:
            LOG.error("weekly card %s failed: %s", kind, exc)
            return 1
        print(f"weekly card {kind} ready in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
