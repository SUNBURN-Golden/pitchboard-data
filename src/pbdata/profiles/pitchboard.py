"""Pitchboard profile: paid book-review/criticism outlets and open calls (literary magazines, publishers, foundations,
cultural orgs, universities). Rules only (AI extraction is off; cost cap 0)."""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import Dict, List, Optional, Tuple

from ..collect.dates import find_dates
from ..collect.parse import Doc, Link, Segment

EXTRACTOR_VERSION = "pb-rules-1"
LABELER_VERSION = "pb-labels-1"
OCR_EXTRACTOR = "ocr:tesseract+rules"

# Fields a Pitchboard call/outlet must eventually have; importance feeds gap priority.
REQUIRED = {  # predicate -> (subject_type, importance)
    "deadline.closes_on": ("call", 1.0),
    "status.declared": ("call", 0.8),
    "fee.submission": ("outlet", 0.7),
    "compensation.rate": ("outlet", 0.9),
    "rights.simultaneous": ("outlet", 0.4),
    "policy.ai": ("outlet", 0.4),
    "eligibility.region": ("outlet", 0.6),
}
FIELD_CUES = {
    "deadline.closes_on": re.compile(r"(deadline|due date|closes?\b|closing date|마감|접수\s*기간|모집\s*기간)", re.I),
    "status.declared": re.compile(r"(open for|now open|closed to|currently (open|closed)|접수\s*중|모집\s*중|접수\s*마감)", re.I),
    "fee.submission": re.compile(r"(\bfees?\b|free of charge|응모료|참가비|접수비|무료)", re.I),
    "compensation.rate": re.compile(r"(\bpay(s|ment)?\b|\bpaid\b|honorarium|compensation|원고료|고료|사례비)", re.I),
    "rights.simultaneous": re.compile(r"(simultaneous|이중\s*투고|중복\s*투고)", re.I),
    "policy.ai": re.compile(r"(\bAI\b|artificial intelligence|generative|생성형|인공지능)", re.I),
    "eligibility.region": re.compile(r"(eligib|residents?|citizens?|open to|international|worldwide|자격|거주|국적)", re.I),
}
# Where to look for a missing field on the same site (reverse search, site-internal).
FIELD_HINTS = {
    "deadline.closes_on": ["deadline", "dates", "calendar", "마감", "일정", "접수", "공모"],
    "status.declared": ["open", "closed", "접수", "모집"],
    "fee.submission": ["fee", "faq", "submit", "guidelines", "응모료", "참가비"],
    "compensation.rate": ["pay", "payment", "rates", "compensation", "원고료", "사례"],
    "rights.simultaneous": ["simultaneous", "faq", "guidelines", "이중"],
    "policy.ai": ["ai", "policy", "guidelines", "생성형"],
    "eligibility.region": ["eligibility", "who", "guidelines", "자격", "요강"],
}

LIT_EN = re.compile(r"\b(literary|literature|fiction|poetry|poems?|essays?|criticism|critic|book reviews?|reviews?|magazine|journal|quarterly|press|publisher|manuscripts?|prize|award|contest|submissions?|writers?|translation|nonfiction|memoir)\b", re.I)
LIT_KO = re.compile(r"(문학|문예|소설|시인|평론|비평|서평|출판|재단|공모|신인상|문학상|투고|원고|작가|번역|창작)")
CALL_EN = re.compile(r"(call for (submissions?|entries|papers|pitches|manuscripts|proposals|applications)|submissions? (are |is )?(now |currently )?open|open call|(now |currently )?accepting (submissions?|entries|pitches|manuscripts)|submit your|open for submissions?|open to submissions?|reading period)", re.I)
CALL_KO = re.compile(r"(공모|모집|투고\s*안내|원고\s*모집|접수|응모|신인상|당선작\s*모집)")
RULES_EN = re.compile(r"(submission guidelines|guidelines|eligibility|rules|terms and conditions|how to submit|faq)", re.I)
RULES_KO = re.compile(r"(규정|응모\s*요강|투고\s*규정|심사\s*기준|응모\s*자격|모집\s*요강|참가\s*자격|유의\s*사항)")
RESULT_EN = re.compile(r"(winners?\b|winning (entry|entries|poems?|stories|story)|shortlist(ed)?|longlist(ed)?|finalists?|(announces?|announced|announcing) (the )?(winners?|results|recipients?|laureates?))", re.I)
RESULT_KO = re.compile(r"(수상작|당선작|당선자|수상자|결과\s*발표|합격자|선정\s*결과|최종\s*선정|심사\s*결과|발표합니다)")
CORR_RE = re.compile(r"(\bcorrections?\b|\bcorrected\b|\bupdate[d]?:|\berrata\b|정정|수정\s*공고|변경\s*공고|정오표|일정\s*변경)", re.I)
EXT_RE = re.compile(r"(deadline (has been |is |was )?extended|extended (the )?deadline|extension of the deadline|new deadline|deadline extension|마감\s*(일\s*)?연장|연장\s*공고|기간\s*연장|연장\s*(?:되|됩|합|하였|했))", re.I)
CANC_RE = re.compile(r"(\bcancel(l)?ed\b|\bcancell?ation\b|no longer (accepting|open|being offered)|discontinued|has been (withdrawn|closed permanently)|취소|폐지|중단\s*(되었|합니다))", re.I)
STANDING_RE = re.compile(r"(rolling (basis|submissions?|deadline)|year[- ]round|always open|ongoing submissions?|submit (at )?any ?time|상시\s*(접수|모집|투고))", re.I)
OPEN_RE = re.compile(r"(submissions? (are |is )?(now |currently )?open|(now|currently) (accepting|open)|open for submissions|접수\s*중|모집\s*중|현재\s*접수)", re.I)
CLOSED_RE = re.compile(r"(submissions? (are |is )?(now |currently )?closed|closed to submissions|not (currently )?(accepting|open to) (new )?(submissions?|pitches)|접수\s*마감|모집\s*마감|마감되었|접수\s*종료)", re.I)

DEADLINE_CUE = re.compile(r"(deadline|closes?|closing|due( date| by)?|submit by|submissions? (until|through|by)|no later than|apply by|until|through|(?:submission|entry|entries|application|reading)\s+(?:period|window)|마감|접수\s*기간|모집\s*기간|응모\s*기간|공모\s*기간|투고\s*기간|접수\s*일정|까지|기한)", re.I)
OPEN_DATE_CUE = re.compile(r"(opens?|opening|starts?|begin(s)?|reading period (begins|starts)|접수\s*시작|시작일)", re.I)
RANGE_CUE = re.compile(r"(기간|period|window|between|from)", re.I)
NOT_DEADLINE_CUE = re.compile(r"(announce|winners?|results|notified|notification|published|posted on|publication date|발표|심사|발행|게시일|시상)", re.I)

FEE_AMT_EN = re.compile(r"(?:(?:submission|reading|entry|application|contest|processing) fees?|fees?)[^.\n]{0,40}?(?P<cur>[$£€]|USD|GBP|EUR)\s?(?P<amt>\d+(?:\.\d{1,2})?)|(?P<cur2>[$£€])\s?(?P<amt2>\d+(?:\.\d{1,2})?)\s+(?:submission|reading|entry|application) fee", re.I)
FEE_AMT_KO = re.compile(r"(응모료|참가비|접수비|참가료|심사비|응모비)(?:(?!없|무료|면제|원고료)[^.\n]){0,20}?(?P<amt>\d[\d,]*)\s*(?P<man>만)?\s*원")
PAY_EN = re.compile(r"(?:we pay|pays?|paid|payment|honorarium|compensation|rate|rates)[^.\n]{0,50}?(?P<cur>[$£€]|USD|GBP|EUR)\s?(?P<lo>\d[\d,]*(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(?:[$£€])?\s?(?P<hi>\d[\d,]*(?:\.\d+)?))?(?P<unit>\s*(?:per|/|a)\s*(?:word|piece|article|review|essay|story|poem|page))?", re.I)
PAY_KO = re.compile(r"(원고료|고료|사례비)[^.\n]{0,30}?(?P<amt>\d[\d,]*)\s*(?P<man>만)?\s*원")
UNPAID_EN = re.compile(r"\b(we do not pay|unpaid|no payment|does not pay|do not pay)\b", re.I)
SIM_OK = re.compile(r"simultaneous (submissions?|submitting)[^.\n]{0,40}?(are |is )?(welcome|accepted|allowed|fine|ok(ay)?|permitted)|(welcome|accept|allow)s? simultaneous|(?:이중|동시)\s*투고\s*(가능|허용)", re.I)
SIM_NO = re.compile(r"(no simultaneous (submissions?|submitting)|do(es)? not (accept|consider|allow) simultaneous|please do not submit simultaneously|simultaneous submissions? (are )?(not|aren't)|(?:이중|동시|중복)\s*투고\s*(금지|불가)|중복\s*투고\s*(금지|불가))", re.I)
AI_NO = re.compile(r"((do not|don't|will not|cannot|can't|not) (accept|consider|allow|publish|use)[^.\n]{0,50}(AI|artificial intelligence|generative|ChatGPT|LLM)|(AI|artificial intelligence|generative AI|ChatGPT)[^.\n]{0,50}(not (accepted|allowed|permitted|considered)|prohibited|banned)|no (AI|generative AI)[- ]generated|(생성형\s*AI|인공지능|AI)[^.\n]{0,30}(사용|활용)?[^.\n]{0,10}(금지|불가|제한))", re.I)
AI_MENTION = re.compile(r"\b(generative AI|AI[- ]generated|artificial intelligence|ChatGPT|LLM)\b|생성형\s*AI|인공지능", re.I)
WORLD_RE = re.compile(r"(open to (writers|submissions|everyone) (from )?(everywhere|worldwide|around the world|internationally)|international submissions are welcome|writers worldwide)", re.I)
ELIG_RE = re.compile(r"((open|restricted|limited) to [^.\n]{0,60}(residents|citizens|nationals|resident|members|students|writers|authors)[^.\n]{0,60}|(residents|citizens) of [^.\n]{0,50}|must be (a )?(resident|citizen|currently|enrolled|over|at least)[^.\n]{0,60}|(국내|대한민국)\s*(거주|국민|국적)[^.\n]{0,40}|응모\s*자격[^.\n]{0,60}|지원\s*자격[^.\n]{0,60}|만\s*\d+세\s*이상[^.\n]{0,30})", re.I)
ENTITY_EN = re.compile(r"(?i:presented|hosted|published|sponsored|organized|organised|funded|administered) (?i:by) (?P<n>[A-Z][\w&'’. -]{2,50}?)(?=[.,;|]|\s+(?:and|with|in|for|at)\b|$)")
ENTITY_KO = re.compile(r"(?:주최|주관|후원|발행처|출판사)\s*[:：]\s*(?P<n>[^,;|/\n·()]{2,30})")
APPLY_ANCHOR = re.compile(r"(submit|apply|application|enter now|submission manager|submittable|제출|응모|접수|신청|지원하기)", re.I)
APPLY_HOST = re.compile(r"(submittable\.com|forms\.gle|docs\.google\.com/forms|airtable\.com|typeform\.com|jotform|naver\.me|form\.naver\.com|forms\.office\.com)", re.I)
SNS_HOST = re.compile(r"(instagram\.com|facebook\.com|twitter\.com|x\.com|bsky\.app|youtube\.com|threads\.net|blog\.naver\.com|tiktok\.com)", re.I)
ATTACH_RE = re.compile(r"\.(pdf|hwp|hwpx|docx?|jpe?g|png|webp)(\?|$)", re.I)
CALL_LINK = re.compile(r"(submit|submission|guidelines|call[- ]for|open[- ]call|contribut|write[- ]for|pitch|apply|contest|prize|award|competition|공모|투고|모집|응모|접수|요강|지원)", re.I)
HIGH_VALUE_PATH = re.compile(r"(submission|guideline|call-?for|open-?call|contest|prize|award|pitch|write-?for|contribute|공모|투고|모집|응모|요강)", re.I)
AGGREGATORS = ("duotrope", "submittable.com", "newpages", "pw.org", "writerswrite", "poetryfoundation", "wevity", "thinkcontest", "linkareer", "campuspick", "allcontest", "contestkorea", "reddit.com", "medium.com", "wikipedia.org")
REPOST_RE = re.compile(r"(originally (posted|published) (on|at)|reposted from|via\s+[A-Z]|source:\s*http|출처\s*[:：]|전재|퍼온\s*글)", re.I)

YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


@dataclasses.dataclass
class Ext:
    field: str
    value_state: str
    value: Optional[dict]
    excerpt: str
    locator: dict
    confidence: float = 1.0


def window(text: str, start: int, end: int, width: int = 270) -> str:
    if len(text) <= width:
        return text
    a = max(0, start - (width - (end - start)) // 2)
    return text[a:a + width].strip()


def _money(cur: str, amt: str, man: Optional[str] = None) -> Tuple[str, float]:
    v = float(amt.replace(",", ""))
    if man:
        v *= 10000
    cur = {"$": "USD", "£": "GBP", "€": "EUR", "": "KRW"}.get(cur, cur.upper() or "KRW")
    return cur, v


def extract(doc: Doc, ocr: bool = False) -> List[Ext]:
    out: List[Ext] = []
    src_conf = 0.8 if ocr else 1.0

    def add(field, state, value, seg, m, conf=1.0):
        out.append(Ext(field, state, value, window(seg.text, m.start(), m.end()), dict(seg.locator, seg=seg.idx, kind=seg.kind, heading=seg.heading[:80]), conf * src_conf))

    for seg in doc.segments:
        t = seg.text
        # --- dates
        hits = find_dates(t)
        if hits and DEADLINE_CUE.search(t) and not (NOT_DEADLINE_CUE.search(t) and not re.search(r"deadline|마감|접수|submit|due", t, re.I)):
            cue = DEADLINE_CUE.search(t)
            first, last = hits[0], hits[-1]
            if len(hits) >= 2 and (RANGE_CUE.search(t) or re.search(r"[~∼–]", t)) and last.iso >= first.iso:
                class _M:  # span helper
                    pass
                mm = _M(); mm.start = lambda f=first: f.start; mm.end = lambda l=last: l.end
                add("deadline.opens_on", "stated", {"date": first.iso, "tz_source": "unknown"}, seg, mm)
                add("deadline.closes_on", "stated", _close(last), seg, mm)
            elif len(hits) >= 1:
                for h in hits[-1:] if len(hits) > 1 else hits:
                    mm = _M2(h.start, h.end)
                    if OPEN_DATE_CUE.search(t[max(0, h.start - 40):h.start]) and not DEADLINE_CUE.search(t[max(0, h.start - 40):h.start]):
                        add("deadline.opens_on", "stated", {"date": h.iso, "tz_source": "unknown"}, seg, mm)
                    else:
                        add("deadline.closes_on", "stated", _close(h), seg, mm)
        # --- fee
        m = FEE_AMT_EN.search(t)
        if m and not re.search(r"\b(no|without|free of)\b[^.]{0,20}fee", t[:m.start() + 30], re.I):
            cur, amt = _money(m.group("cur") or m.group("cur2"), m.group("amt") or m.group("amt2"))
            add("fee.submission", "stated", {"amount_min": amt, "amount_max": amt, "currency": cur, "unit": "per_submission", "comparable": True}, seg, m)
        else:
            m = FEE_AMT_KO.search(t)
            if m:
                cur, amt = _money("", m.group("amt"), m.group("man"))
                add("fee.submission", "stated", {"amount_min": amt, "amount_max": amt, "currency": cur, "unit": "per_submission", "comparable": True}, seg, m)
            else:
                from ..model import EXPLICIT_NONE_PATTERNS
                m = EXPLICIT_NONE_PATTERNS["fee.submission"].search(t)
                if m:
                    add("fee.submission", "explicitly_none", {"fee": "none"}, seg, m)
        # --- compensation
        m = PAY_EN.search(t)
        if m and not re.search(r"\b(prize|award|winner)\b", t[:m.start() + 20], re.I):
            lo = float(m.group("lo").replace(",", ""))
            hi = float(m.group("hi").replace(",", "")) if m.group("hi") else lo
            unit = None
            if m.group("unit"):
                unit = "per_" + re.search(r"(word|piece|article|review|essay|story|poem|page)", m.group("unit"), re.I).group(1).lower()
            cur, _ = _money(m.group("cur"), "0")
            add("compensation.rate", "stated", {"amount_min": lo, "amount_max": hi, "currency": cur, "unit": unit, "comparable": bool(unit)}, seg, m)
        else:
            m = PAY_KO.search(t)
            if m:
                cur, amt = _money("", m.group("amt"), m.group("man"))
                add("compensation.rate", "stated", {"amount_min": amt, "amount_max": amt, "currency": cur, "unit": None, "comparable": False}, seg, m)
            else:
                m = UNPAID_EN.search(t)
                if m:
                    add("compensation.rate", "explicitly_none", {"pay": "none"}, seg, m)
        # --- simultaneous
        m = SIM_NO.search(t)
        if m:
            add("rights.simultaneous", "stated", {"state": "not_allowed"}, seg, m)
        else:
            m = SIM_OK.search(t)
            if m:
                add("rights.simultaneous", "stated", {"state": "allowed"}, seg, m)
        # --- AI policy
        m = AI_NO.search(t)
        if m:
            add("policy.ai", "stated", {"dimension": "generated_content", "state": "prohibited"}, seg, m)
        elif AI_MENTION.search(t) and len(t) < 600:
            m = AI_MENTION.search(t)
            add("policy.ai", "stated", {"dimension": "generated_content", "state": "mentioned_unclassified"}, seg, m, 0.5)
        # --- eligibility
        m = WORLD_RE.search(t)
        if m:
            add("eligibility.region", "explicitly_none", {"scope": "worldwide"}, seg, m)
        else:
            m = ELIG_RE.search(t)
            if m:
                add("eligibility.statement", "stated", {"text": window(t, m.start(), m.end(), 200)}, seg, m, 0.7)
        # --- status
        for rx, st in ((CLOSED_RE, "closed"), (OPEN_RE, "open"), (STANDING_RE, "standing"), (CANC_RE, "cancelled")):
            m = rx.search(t)
            if m:
                add("status.declared", "stated", {"declared": st}, seg, m, 0.9 if st != "cancelled" else 0.6)
                break
        # --- entities
        for rx in (ENTITY_EN, ENTITY_KO):
            for m in rx.finditer(t):
                name = " ".join(m.group("n").split())
                if 2 < len(name) < 60:
                    add("signal.entity", "stated", {"name": name}, seg, m, 0.6)
    have = {e.field for e in out}
    for pred, rx in FIELD_CUES.items():
        if pred in have:
            continue
        for seg in doc.segments:
            m = rx.search(seg.text)
            if m:
                out.append(Ext("signal.cue", "not_inspected", {"for": pred}, window(seg.text, m.start(), m.end()),
                               dict(seg.locator, seg=seg.idx, kind=seg.kind), 0.5))
                break
    return out


class _M2:
    def __init__(self, s, e):
        self._s, self._e = s, e

    def start(self):
        return self._s

    def end(self):
        return self._e


def _close(h) -> dict:
    v = {"date": h.iso, "tz_source": "explicit" if h.offset is not None else "unknown"}
    if h.offset is not None:
        v["utc_offset_hours"] = h.offset
    if h.time:
        v["time"] = h.time
    return v


def link_signals(doc: Doc) -> List[Ext]:
    out = []
    seen = set()
    for l in doc.links:
        if l.href in seen or not l.href.startswith("http"):
            continue
        seen.add(l.href)
        if SNS_HOST.search(l.href):
            out.append(Ext("signal.sns", "stated", {"url": l.href}, l.text or l.href[:120], {"link": True}, 1.0))
        elif ATTACH_RE.search(l.href):
            out.append(Ext("signal.attachment", "stated", {"url": l.href, "kind": ATTACH_RE.search(l.href).group(1).lower()}, l.text or l.href[:120], {"link": True}))
        elif APPLY_HOST.search(l.href) or APPLY_ANCHOR.search(l.text or ""):
            out.append(Ext("signal.apply_url", "stated", {"url": l.href}, l.text or l.href[:120], {"link": True}))
    return out


# ------------------------------------------------------------------ labels
@dataclasses.dataclass
class LabelResult:
    label: str
    basis: str
    status: str
    open_call_eligible: bool
    rules: List[str]
    years: List[int]


def label_doc(doc: Doc, exts: List[Ext], today: dt.date, source_official: bool = True) -> List[LabelResult]:
    title = " ".join([doc.title] + [s.text for s in doc.segments if s.kind == "h"][:6])
    body = doc.text()
    years = sorted({int(y) for y in YEAR_RE.findall(title + " " + body[:20000])})
    closes = sorted({e.value["date"] for e in exts if e.field == "deadline.closes_on" and e.value})
    labels: List[Tuple[str, str, List[str]]] = []

    def hit(rx_en, rx_ko, name):
        rules, basis = [], None
        if rx_en.search(title) or (rx_ko and rx_ko.search(title)):
            rules.append(name + ":title")
            basis = "fact" if source_official else "claim"
        heads = " ".join(s.text for s in doc.segments if s.kind == "h")
        if rx_en.search(heads) or (rx_ko and rx_ko.search(heads)):
            rules.append(name + ":heading")
            basis = basis or "claim"
        if rx_en.search(body) or (rx_ko and rx_ko.search(body)):
            rules.append(name + ":body")
            basis = basis or "inference"
        return (basis, rules) if rules else (None, [])

    found = {}
    for name, en, ko in (("recruitment", CALL_EN, CALL_KO), ("rules", RULES_EN, RULES_KO), ("results", RESULT_EN, RESULT_KO),
                         ("correction", CORR_RE, None), ("extension", EXT_RE, None), ("cancellation", CANC_RE, None)):
        basis, rules = hit(en, ko, name)
        if basis:
            found[name] = (basis, rules)
    # standing/open evidence strengthens recruitment
    if "recruitment" not in found and (STANDING_RE.search(body) or OPEN_RE.search(body)):
        found["recruitment"] = ("inference", ["recruitment:status_cue"])
    if "recruitment" not in found and closes and re.search(r"(submissions?|entries|entry|apply|application|응모|접수|제출|원고)", body, re.I) \
            and "results" not in found:
        found["recruitment"] = ("inference", ["recruitment:deadline_with_submission_cue"])
    # results announcements in the title dominate: an award announcement is never an open call
    results_primary = "results" in found and any(r.endswith(":title") for r in found["results"][1]) \
        and not any(r.endswith(":title") for r in found.get("recruitment", ("", []))[1])
    today_s = today.isoformat()
    out: List[LabelResult] = []
    for name, (basis, rules) in found.items():
        status, eligible = "n/a", False
        if name == "recruitment":
            if results_primary:
                status = "results_not_call"
            elif closes:
                if closes[-1] >= today_s:
                    status, eligible = "current", True
                else:
                    status = "stale_prior_year" if int(closes[-1][:4]) < today.year else "past_deadline"
            elif years and max(years) < today.year:
                status = "stale_prior_year"
            elif STANDING_RE.search(body):
                status, eligible = "current", True
            else:
                status = "unknown_period"
            if "cancellation" in found and not results_primary and CANC_RE.search(title):
                eligible = False
        out.append(LabelResult(name, basis, status, eligible, rules, years))
    if not out:
        out.append(LabelResult("other", "inference", "n/a", False, [], years))
    return out


def relevance(texts: List[str], lang: str, target: dict) -> float:
    blob = " ".join(texts)[:30000]
    n_en = len(LIT_EN.findall(blob))
    n_ko = len(LIT_KO.findall(blob))
    kw = [k.lower() for k in target.get("keywords", [])]
    n_kw = sum(1 for k in kw if k and k in blob.lower())
    score = min(1.0, 0.12 * min(n_en, 4) + 0.12 * min(n_ko, 4) + 0.1 * min(n_kw, 3))
    if CALL_EN.search(blob) or CALL_KO.search(blob):
        score += 0.35
    tl = (target.get("language") or "").lower()
    if tl and lang and not lang.startswith(tl) and not (tl == "ko" and n_ko > 2):
        score *= 0.6
    return round(min(1.0, score), 3)


NEWS_PATH = re.compile(r"(news|notice|announce|board|bbs|competitions?|contests?|prizes?|awards?|opportunit|공지|소식|알림|공고|사업안내)", re.I)
NEWEST_CUE = re.compile(r"(\bnew\b|latest|now open|open now|20\d\d[- ]?20\d\d|신규|최신|접수\s*중|모집\s*중)", re.I)


def cycle_score(text: str, today: Optional[dt.date] = None) -> float:
    """Current-cycle preference: +0.5 for the current/next year in URL or anchor, -0.8 if only past years appear."""
    y = (today or dt.date.today()).year
    years = {int(v) for v in YEAR_RE.findall(text or "")}
    if not years:
        return 0.0
    if max(years) >= y:
        return 0.5 if max(years) <= y + 1 else 0.0
    return -0.8


def link_priority(link: Link, today: Optional[dt.date] = None) -> float:
    s = cycle_score((link.href or "") + " " + (link.text or ""), today)
    if NEWS_PATH.search(link.href) or NEWS_PATH.search(link.text or ""):
        s += 0.2
    if NEWEST_CUE.search(link.text or ""):
        s += 0.2
    if HIGH_VALUE_PATH.search(link.href):
        s += 0.6
    if CALL_LINK.search(link.text or ""):
        s += 0.5
    if APPLY_HOST.search(link.href):
        s += 0.2
    return s


def scope_from_text(text: str) -> str:
    t = text.lower()
    if re.search(r"book reviews?|서평", t):
        return "book_review"
    if re.search(r"critical essay|criticism|평론|비평", t):
        return "critical_essay"
    return "unspecified"
