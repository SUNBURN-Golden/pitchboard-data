"""SVG layouts for The Pitchboard's weekly share cards."""

from decimal import Decimal
from html import escape
import re

# Ported from VIRAL's brand-cards-1120/SPEC.md §C2 and generate_assets.py.
BG = "#faf8f4"
SURFACE = "#ffffff"
TEXT = "#1c1b19"
MUTED = "#6b675f"
BORDER = "#e3ded3"
ACCENT = "#8a2a1c"

SERIF = "Source Serif Pro"
SANS = "Inter"

CURRENCY_CODE = r"(?:USD|CAD|AUD|GBP|EUR)"
CURRENCY = rf"(?:{CURRENCY_CODE}\s*)?[$£€¥₹]"
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
AMOUNT = re.compile(
    rf"(?:~?{CURRENCY}\s*(?P<first>{NUMBER})"
    rf"(?:\s*[-–—]\s*(?:{CURRENCY}\s*)?(?P<upper>{NUMBER}))?\+?"
    rf"|(?P<before_code>{NUMBER})\s+{CURRENCY_CODE}\b)",
    re.I,
)


def pin_mark(cx: float, cy: float, scale: float = 1.0, hole: str = BG) -> str:
    """Flat terracotta map-pin. Local design size ~20 units tall at scale=1.

    Head centered slightly above cy; needle points down. cy is the visual center
    of the whole pin (head + needle), not the head alone.
    """
    # Head center is a bit above visual center; tip below.
    hx, hy = cx, cy - 3.1 * scale
    hr = 5.2 * scale
    hole_r = 1.65 * scale
    tip_y = cy + 9.6 * scale
    # Needle attaches under the head
    attach_y = hy + hr * 0.72
    half = 3.05 * scale
    return (
        f'<g class="pin">'
        f'<path d="M{hx - half:.2f} {attach_y:.2f} L{hx:.2f} {tip_y:.2f} L{hx + half:.2f} {attach_y:.2f} Z" fill="{ACCENT}"/>'
        f'<circle cx="{hx:.2f}" cy="{hy:.2f}" r="{hr:.2f}" fill="{ACCENT}"/>'
        f'<circle cx="{hx:.2f}" cy="{hy:.2f}" r="{hole_r:.2f}" fill="{hole}"/>'
        f"</g>"
    )


def svg_start(w: int, h: int, title: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" aria-label="{title}">\n'
        f"<title>{title}</title>\n"
    )


def svg_end() -> str:
    return "</svg>\n"


def card_header(w: int, title: str, kicker: str, subtitle: str | None, header_bottom: int) -> str:
    # pin + kicker on one line, title below, optional subtitle, then a rule
    parts = [
        f'<rect width="{w}" height="100%" fill="{BG}"/>',
        pin_mark(48 + 14, 44, scale=1.35, hole=BG) if w == 1200
        else pin_mark(56 + 16, 56, scale=1.55, hole=BG),
    ]
    if w == 1200:
        parts.append(
            f'<text x="96" y="50" font-family="{SANS}" font-weight="700" '
            f'font-size="13" letter-spacing="2.4" fill="{ACCENT}">{kicker}</text>'
        )
        parts.append(
            f'<text x="48" y="96" font-family="{SERIF}" font-size="34" fill="{TEXT}">{title}</text>'
        )
        if subtitle:
            # landscape: subtitle on the right of the kicker row to save height
            parts.append(
                f'<text x="{w - 48}" y="50" text-anchor="end" font-family="{SANS}" '
                f'font-weight="500" font-size="13" fill="{MUTED}">{subtitle}</text>'
            )
        parts.append(
            f'<line x1="48" y1="{header_bottom}" x2="{w - 48}" y2="{header_bottom}" '
            f'stroke="{BORDER}" stroke-width="1.5"/>'
        )
    else:
        parts.append(
            f'<text x="112" y="62" font-family="{SANS}" font-weight="700" '
            f'font-size="14" letter-spacing="2.6" fill="{ACCENT}">{kicker}</text>'
        )
        parts.append(
            f'<text x="56" y="126" font-family="{SERIF}" font-size="48" fill="{TEXT}">{title}</text>'
        )
        if subtitle:
            parts.append(
                f'<text x="56" y="164" font-family="{SANS}" font-weight="400" '
                f'font-size="18" fill="{MUTED}">{subtitle}</text>'
            )
        parts.append(
            f'<line x1="56" y1="{header_bottom}" x2="{w - 56}" y2="{header_bottom}" '
            f'stroke="{BORDER}" stroke-width="1.5"/>'
        )
    return "\n".join(parts)


def card_footer(w: int, h: int, date_label: str, note: str = "") -> str:
    margin = 48 if w == 1200 else 56
    dated = f"Rates as listed on {date_label}."
    terms = "Check each outlet for current terms."
    if note:
        terms += f"  ·  {note}"
    if w == 1200:
        return (
            f'<line x1="{margin}" y1="{h - 52}" x2="{w - margin}" y2="{h - 52}" '
            f'stroke="{BORDER}" stroke-width="1.5"/>'
            f'<text x="{margin}" y="{h - 28}" font-family="{SANS}" font-weight="400" '
            f'font-size="13" fill="{MUTED}">{dated} {terms}</text>'
        )
    return (
        f'<line x1="{margin}" y1="{h - 88}" x2="{w - margin}" y2="{h - 88}" '
        f'stroke="{BORDER}" stroke-width="1.5"/>'
        f'<text x="{margin}" y="{h - 56}" font-family="{SANS}" font-weight="400" '
        f'font-size="15" fill="{MUTED}">{dated}</text>'
        f'<text x="{margin}" y="{h - 34}" font-family="{SANS}" font-weight="500" '
        f'font-size="14" fill="{MUTED}">{terms}</text>'
    )


def ellipsize(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    # Include the ellipsis in the limit and keep only whole words.
    prefix = value[:limit - 1]
    if not value[limit - 1].isspace() and not prefix[-1:].isspace():
        prefix = prefix.rsplit(None, 1)[0] if len(prefix.split()) > 1 else ""
    return prefix.rstrip() + "…"


def portrait_lines(value: str, first_line_limit: int, total_limit: int) -> list[str]:
    value = ellipsize(value, total_limit)
    if len(value) <= first_line_limit:
        return [value]
    # If a single word exceeds the first-line limit, keep it intact.
    split = value.rfind(" ", 0, first_line_limit + 1)
    if split == -1:
        split = value.find(" ")
    return [value[:split], value[split + 1:]] if split != -1 else [value]


def portrait_text(value: str, line_limit: int, total_limit: int, baseline: int, size: int) -> str:
    lines = portrait_lines(value, line_limit, total_limit)
    leading = size
    first_y = baseline - leading * (len(lines) - 1)
    return "".join(
        f'<text x="140" y="{first_y + i * leading}" font-family="{SANS}" font-weight="500" '
        f'font-size="{size}" fill="{TEXT}">{escape(line)}</text>'
        for i, line in enumerate(lines)
    )


def listed_amount(match):
    amount = match["upper"] or match["first"] or match["before_code"]
    return Decimal(amount.replace(",", ""))


def compact_rate(value: str, limit: int, *, suffix: str = "", sort_value=None) -> str:
    if len(value) + len(suffix) <= limit:
        return value + suffix
    budget = limit - len(suffix)
    shortened = ellipsize(value, budget)
    amounts = list(AMOUNT.finditer(value))
    if not amounts:
        return shortened + suffix
    if sort_value is None:
        match = amounts[0]
    else:
        # Select a verbatim part of the listing. For rates written in another
        # currency, use its stated upper amount without converting it.
        match = next((amount for amount in amounts if listed_amount(amount) == sort_value), None)
        if match is None:
            match = max(amounts, key=listed_amount)
    amount = match[0]
    if amount not in shortened:
        if len(amount) <= budget:
            shortened = amount + ("…" if len(amount) < budget else "")
        elif suffix and len(amount) <= limit:
            return amount + ("…" if len(amount) < limit and amount != value else "")
    return shortened + suffix


def top10_x_svg(rows, date_label: str, note: str) -> str:
    w, h = 1200, 675
    header_bottom = 120
    row_h = 46
    # 10 rows from y=120 to y=580; footer starts ~623
    parts = [
        svg_start(w, h, "The Pitchboard — Top 10 rates as listed"),
        card_header(
            w,
            "Top 10 rates as listed",
            "THE PITCHBOARD",
            "Directory list  ·  not an endorsement",
            header_bottom,
        ),
    ]
    for i, row in enumerate(rows):
        y0 = header_bottom + i * row_h
        rank = f"{i + 1:02d}"
        name = escape(ellipsize(row["outlet_name"], 32))
        rate = escape(compact_rate(row["rate_as_listed"], 14, sort_value=row["pay_usd_max"]))
        unit = escape(ellipsize(row["unit"], 18))
        # faint zebra
        if i % 2 == 0:
            parts.append(
                f'<rect x="48" y="{y0}" width="1104" height="{row_h}" fill="{SURFACE}" opacity="0.7"/>'
            )
        parts.append(
            f'<text x="64" y="{y0 + 29}" font-family="{SANS}" font-weight="600" '
            f'font-size="15" fill="{MUTED}">{rank}</text>'
        )
        parts.append(
            f'<text x="112" y="{y0 + 21}" font-family="{SANS}" font-weight="500" '
            f'font-size="18" fill="{TEXT}">{name}</text>'
        )
        if unit:
            parts.append(
                f'<text x="112" y="{y0 + 38}" font-family="{SANS}" font-weight="400" '
                f'font-size="12" fill="{MUTED}">{unit}</text>'
            )
        parts.append(
            f'<text x="1152" y="{y0 + 30}" text-anchor="end" font-family="{SERIF}" '
            f'font-size="22" fill="{TEXT}">{rate}</text>'
        )
        if i < len(rows) - 1:
            parts.append(
                f'<line x1="48" y1="{y0 + row_h}" x2="1152" y2="{y0 + row_h}" '
                f'stroke="{BORDER}" stroke-width="1"/>'
            )
    parts.append(card_footer(w, h, date_label, note))
    parts.append(svg_end())
    return "\n".join(parts)


def top10_portrait_svg(rows, date_label: str, note: str) -> str:
    w, h = 1080, 1350
    header_bottom = 196
    row_h = 102
    parts = [
        svg_start(w, h, "The Pitchboard — Top 10 rates as listed"),
        card_header(
            w,
            "Top 10 rates as listed",
            "THE PITCHBOARD",
            "Highest stated range in this directory. Not an endorsement.",
            header_bottom,
        ),
    ]
    for i, row in enumerate(rows):
        y0 = header_bottom + i * row_h
        rank = f"{i + 1:02d}"
        rate = escape(compact_rate(row["rate_as_listed"], 14, sort_value=row["pay_usd_max"]))
        unit = escape(ellipsize(row["unit"], 18))
        if i % 2 == 0:
            parts.append(
                f'<rect x="56" y="{y0}" width="968" height="{row_h}" fill="{SURFACE}" opacity="0.7"/>'
            )
        parts.append(
            f'<text x="80" y="{y0 + 60}" font-family="{SANS}" font-weight="600" '
            f'font-size="18" fill="{MUTED}">{rank}</text>'
        )
        parts.append(portrait_text(row["outlet_name"], 32, 40, y0 + 48, 26))
        if unit:
            parts.append(
                f'<text x="140" y="{y0 + 76}" font-family="{SANS}" font-weight="400" '
                f'font-size="16" fill="{MUTED}">{unit}</text>'
            )
        parts.append(
            f'<text x="1024" y="{y0 + 64}" text-anchor="end" font-family="{SERIF}" '
            f'font-size="32" fill="{TEXT}">{rate}</text>'
        )
        if i < len(rows) - 1:
            parts.append(
                f'<line x1="56" y1="{y0 + row_h}" x2="1024" y2="{y0 + row_h}" '
                f'stroke="{BORDER}" stroke-width="1"/>'
            )
    parts.append(card_footer(w, h, date_label, note))
    parts.append(svg_end())
    return "\n".join(parts)


def deadline_meta(row) -> str:
    outlet = ellipsize(row["outlet_name"], 28)
    suffix = " as listed" if row["has_listed_amount"] else ""
    rate = compact_rate(row["rate_as_listed"], 20, suffix=suffix)
    return escape(f'{outlet}  ·  closes {row["deadline_date"]}  ·  {rate}')


def sponsor_label(row, right: int, baseline: int) -> str:
    label = row["sponsorship"]
    if not label:
        return ""
    return (
        f'<text x="{right}" y="{baseline}" text-anchor="end" font-family="{SANS}" '
        f'font-weight="600" font-size="13" fill="{ACCENT}">{label}</text>'
    )


def deadlines_x_svg(rows, date_label: str, note: str) -> str:
    w, h = 1200, 675
    header_bottom = 120
    row_h = 58
    parts = [
        svg_start(w, h, "The Pitchboard — This week’s deadlines"),
        card_header(
            w,
            "This week’s deadlines",
            "THE PITCHBOARD",
            "Next 7 days  ·  not an endorsement",
            header_bottom,
        ),
    ]
    for i, row in enumerate(rows):
        y0 = header_bottom + i * row_h
        rank = f"{i + 1:02d}"
        title = escape(ellipsize(row["call_title"], 42))
        if i % 2 == 0:
            parts.append(
                f'<rect x="48" y="{y0}" width="1104" height="{row_h}" fill="{SURFACE}" opacity="0.7"/>'
            )
        parts.append(
            f'<text x="64" y="{y0 + 36}" font-family="{SANS}" font-weight="600" '
            f'font-size="15" fill="{MUTED}">{rank}</text>'
        )
        parts.append(
            f'<text x="112" y="{y0 + 26}" font-family="{SANS}" font-weight="500" '
            f'font-size="20" fill="{TEXT}">{title}</text>'
        )
        parts.append(
            f'<text x="112" y="{y0 + 48}" font-family="{SANS}" font-weight="400" '
            f'font-size="14" fill="{MUTED}">{deadline_meta(row)}</text>'
        )
        parts.append(sponsor_label(row, 1152, y0 + 26))
        if i < len(rows) - 1:
            parts.append(
                f'<line x1="48" y1="{y0 + row_h}" x2="1152" y2="{y0 + row_h}" '
                f'stroke="{BORDER}" stroke-width="1"/>'
            )
    parts.append(card_footer(w, h, date_label, note))
    parts.append(svg_end())
    return "\n".join(parts)


def deadlines_portrait_svg(rows, date_label: str, note: str) -> str:
    w, h = 1080, 1350
    header_bottom = 196
    row_h = 102
    parts = [
        svg_start(w, h, "The Pitchboard — This week’s deadlines"),
        card_header(
            w,
            "This week’s deadlines",
            "THE PITCHBOARD",
            "Deadlines in the next 7 days, as listed. Not an endorsement.",
            header_bottom,
        ),
    ]
    for i, row in enumerate(rows):
        y0 = header_bottom + i * row_h
        rank = f"{i + 1:02d}"
        if i % 2 == 0:
            parts.append(
                f'<rect x="56" y="{y0}" width="968" height="{row_h}" fill="{SURFACE}" opacity="0.7"/>'
            )
        parts.append(
            f'<text x="80" y="{y0 + 60}" font-family="{SANS}" font-weight="600" '
            f'font-size="18" fill="{MUTED}">{rank}</text>'
        )
        parts.append(portrait_text(row["call_title"], 42, 56, y0 + 46, 24))
        parts.append(
            f'<text x="140" y="{y0 + 76}" font-family="{SANS}" font-weight="400" '
            f'font-size="16" fill="{MUTED}">{deadline_meta(row)}</text>'
        )
        parts.append(sponsor_label(row, 1024, y0 + 46))
        if i < len(rows) - 1:
            parts.append(
                f'<line x1="56" y1="{y0 + row_h}" x2="1024" y2="{y0 + row_h}" '
                f'stroke="{BORDER}" stroke-width="1"/>'
            )
    parts.append(card_footer(w, h, date_label, note))
    parts.append(svg_end())
    return "\n".join(parts)
