"""Pitchboard profile: paid book-review/criticism outlets and open calls (literary magazines, publishers, foundations,
cultural orgs, universities). Rules only (AI extraction is off; cost cap 0)."""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import Dict, List, Optional, Tuple

from ..collect.dates import find_dates
from ..collect.parse import Doc, Link, Segment

EXTRACTOR_VERSION = "pb-rules-6"   # 6: scholarship/student-finance gate with writing-cue override; non-writing page titles gate deadlines
LABELER_VERSION = "pb-labels-3"    # 3: non-writing titles also block rules-only call creation, even without recruitment cues
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
# Korean: a bare "접수 마감" / "모집 마감" is usually a schedule label ("접수 마감 : 11월 5일", "접수 마감일", "모집마감시까지"),
# not a declaration that the call is closed; only count it when no date/label/time word follows (pb-status-1)
_KO_CLOSE_LABEL_AFTER = r"(?!\s*(?:일|시|기한|기간|일정|일시|예정|전|후|까지|이전|이후|[:：|~\-]|\(|\d)|\s*$)"
CLOSED_RE = re.compile(r"(submissions? (are |is )?(now |currently )?closed|closed to submissions|not (currently )?(accepting|open to) (new )?(submissions?|pitches)|"
                       r"(?:접수|모집)\s*(?:이\s*)?마감(?:되었|됐|되어|됨|했|하였|합니다|입니다)|(?:접수|모집)\s*마감" + _KO_CLOSE_LABEL_AFTER + r"|"
                       r"마감되었|접수\s*(?:이\s*)?종료(?:되었|됐|됨|했|하였|합니다|입니다)|접수\s*종료" + _KO_CLOSE_LABEL_AFTER + r")", re.I)
# status.declared "cancelled": the CALL itself must be the thing cancelled. Korean pages are full of other 취소 (수상 취소,
# 선정 취소, 참가 취소, 예매/뉴스레터 취소, 저장 취소 buttons, conditional "취소될 수 있음") that are never a call cancellation.
STATUS_CANC_EN = re.compile(r"((?:contest|competition|call|prize|award|issue|reading period|submissions?)\s+(?:has been |have been |was |were |is )?(?:cancell?ed|discontinued)|"
                            r"no longer (accepting|open|being offered)|has been (withdrawn|closed permanently))", re.I)
STATUS_CANC_KO = re.compile(r"(?<![가-힣])(?:공모전?|모집|접수|대회|사업|행사|선발|공고)\s*(?:이|가|은|는|을|를)?\s*(?:전면\s*)?(?:취소|폐지|중단)"
                            r"(?:\s*(?:되었|됐|되어|됨|합니다|하였|했|함|공고|안내|공지|알림))")
STATUS_CANC_COND = re.compile(r"(경우|시에는|시\s|(?<!전)면\s|때|수\s*있|될\s*수|예정)")


def status_cancel_match(text: str):
    """A cancellation declaration for the call itself, or None (conditional/penalty clauses are not declarations)."""
    for rx in (STATUS_CANC_EN, STATUS_CANC_KO):
        for m in rx.finditer(text):
            ctx = text[max(0, m.start() - 20):m.end() + 12]
            if rx is STATUS_CANC_KO and STATUS_CANC_COND.search(ctx):
                continue
            return m
    return None

DEADLINE_CUE = re.compile(r"(deadline|closes?|closing|due( date| by)?|submit by|submissions? (until|through|by)|no later than|apply by|until|through|(?:submission|entry|entries|application|reading)\s+(?:period|window)|마감|접수\s*기간|모집\s*기간|응모\s*기간|공모\s*기간|투고\s*기간|접수\s*일정|까지|기한|"
                          r"(?:신청|지원|제출|응모|원서\s*접수)\s*(?:기간|기한|마감|일정)|(?:지원|원서|온라인|우편|방문)\s*접수|접수\s*(?:일시|마감|기한)|"
                          r"(?:^|[|│])\s*(?:신청|접수|모집|응모|공모)\s*[:：|]?\s*(?=(?:19|20)\d{2}\s*(?:[-./]|년)))", re.I)
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
# eligibility.region as a stated value (pb-region-1). Before this, the extractor only ever wrote the narrow worldwide
# explicitly_none phrase, so eligibility.region 'stated' was 0 on every page. Values stay literal: a place is only
# recorded when it resolves to a known country; anything else stays an eligibility.statement for human reading.
PLACES = [  # (regex, code)
    (r"the united states(?: of america)?|united states(?: of america)?|u\.\s?s\.(?:\s?a\.)?|usa|america", "US"),
    (r"the united kingdom|united kingdom|\buk\b|u\.k\.|great britain|britain|england|scotland|wales|northern ireland", "GB"),
    (r"(?:the republic of )?ireland", "IE"), (r"canada", "CA"), (r"australia", "AU"), (r"new zealand|aotearoa", "NZ"),
    (r"(?:south |the republic of )?korea", "KR"), (r"india", "IN"), (r"south africa", "ZA"), (r"nigeria", "NG"),
    (r"singapore", "SG"), (r"the philippines|philippines", "PH"), (r"namibia", "NA"), (r"kenya", "KE"), (r"ghana", "GH"),
    (r"jamaica", "JM"), (r"syria", "SY"), (r"iran", "IR"), (r"cuba", "CU"), (r"north korea", "KP"), (r"russia", "RU"),
    (r"crimea", "UA-43"), (r"belarus", "BY"), (r"venezuela", "VE"), (r"sudan", "SD"),
]
DEMONYMS = {"american": "US", "u.s.": "US", "us": "US", "british": "GB", "uk": "GB", "u.k.": "GB", "irish": "IE", "canadian": "CA",
            "australian": "AU", "new zealand": "NZ", "korean": "KR"}
_PLACE_ALT = "|".join(r for r, _ in PLACES)
_DEMO_ALT = r"american|(?-i:U\.S\.|US)|british|(?-i:UK|U\.K\.)|irish|canadian|australian|new zealand|korean"
REGION_WORLD_EN = re.compile(r"(open to (?:both )?(?:u\.?s\.?|american) and non-(?:u\.?s\.?|american) (?:residents|citizens|writers|entrants|authors)|"
                             r"(?:writers|authors|poets|submissions|entries|applicants) (?:of|from) (?:every|any|all) (?:nationalit(?:y|ies)|countr(?:y|ies))|"
                             r"regardless of (?:your |their )?(?:nationality|citizenship|country of residence|location|where (?:you|they) live)|"
                             r"open to (?:writers|authors|poets|entrants|applicants|anyone|everyone) (?:from |in )?(?:anywhere(?: in the world)?|any country|all countries|around the world|across the (?:world|globe))|"
                             r"(?:accept|welcome)s? (?:submissions|entries|work|writers) from (?:anywhere|around the world|all over the world|all countries|any country))", re.I)
REGION_EXCL_EN = re.compile(r"(?:unable to|cannot|can't|can ?not|do not|don't|does not|will not) accept (?:entries|submissions|work|applications) from (?P<list>[^.;]{3,200})", re.I)
REGION_ONLY_EN = re.compile(r"((?:open|restricted|limited|available)\s+(?:only\s+)?to\s+(?:writers\s+|authors\s+|poets\s+|applicants\s+|entrants\s+|creators\s+)?(?:who\s+are\s+)?"
                            r"(?:legal\s+|permanent\s+|current\s+)?(?:residents?|citizens?|nationals?)\s+of\s+(?P<p1>" + _PLACE_ALT + r")|"
                            r"must\s+(?:be|have\s+been)\s+(?:a\s+|an\s+)?(?:legal\s+|permanent\s+|current\s+)?(?:residents?|citizens?|nationals?)\s+of\s+(?P<p2>" + _PLACE_ALT + r")|"
                            r"open\s+(?:only\s+)?to\s+(?:writers|authors|poets|applicants|creators|entrants)\s+(?:living|based|residing|resident)\s+in\s+(?P<p3>" + _PLACE_ALT + r")|"
                            r"(?P<d1>" + _DEMO_ALT + r")\s+(?:residents|citizens|writers|authors)\s+only|"
                            r"open\s+(?:only\s+)?to\s+(?P<d2>" + _DEMO_ALT + r")\s+(?:residents|citizens|nationals|writers|authors|poets))", re.I)
REGION_WORLD_KO = re.compile(r"(국적\s*(?:및\s*거주지\s*)?(?:불문|무관|제한\s*(?:없|이\s*없))|거주\s*(?:지역|지|국가)\s*(?:불문|무관|제한\s*(?:없|이\s*없))|전\s*세계\s*누구나)")
REGION_KR_KO = re.compile(r"((?:대한민국|한국)\s*(?:국적(?:을\s*(?:가진|소지한|보유한))?(?:자|인)?|국민|시민권자)|(?:국내|대한민국|한국)\s*(?:에\s*)?(?:거주(?:하는|자|중인)?|체류(?:하는|자)?))")
REGION_OVERSEAS_KO = re.compile(r"(재외\s*(?:동포|국민|한인)|해외\s*(?:거주(?:자|하는)?\s*(?:한국인|한인|동포|교민)?|교포|동포|교민))[^.\n]{0,20}(포함|가능|도\s*(?:응모|지원|참가))")
REGION_FOREIGN_KO = re.compile(r"(외국인|외국\s*국적(?:자|을\s*가진)?)")
ELIG_CUE_KO = re.compile(r"(자격|대상|응모|지원|참가|신청|공모)")


def _places(text: str) -> List[str]:
    found = []
    t = " " + text.lower() + " "
    for rx, code in sorted(PLACES, key=lambda x: -len(x[0])):
        if re.search(r"(?<![a-z])(?:" + rx + r")(?![a-z])", t, re.I):
            # 'north korea' must not also count as KR
            if code == "KR" and re.search(r"north korea", t):
                t2 = re.sub(r"north korea", " ", t)
                if not re.search(r"(?<![a-z])(?:" + rx + r")(?![a-z])", t2, re.I):
                    continue
            if code not in found:
                found.append(code)
    return found


def region_value(text: str) -> Optional[Tuple[str, dict, "re.Match"]]:
    """eligibility.region read literally from one segment: ('stated', value, match) or None.
    value: {scope: worldwide|restricted, regions?, excluded?, basis?, korea_resident_eligible: True|False|None}."""
    m = REGION_WORLD_EN.search(text) or REGION_WORLD_KO.search(text)
    ex = REGION_EXCL_EN.search(text)
    if m or (ex and _places(ex.group("list"))):
        excluded = _places(ex.group("list")) if ex else []
        return "stated", {"scope": "worldwide", "worldwide": True, "excluded": excluded,
                          "korea_resident_eligible": "KR" not in excluded}, (m or ex)
    m = REGION_ONLY_EN.search(text)
    if m:
        p = next((g for g in (m.group("p1"), m.group("p2"), m.group("p3")) if g), None)
        regions = _places(p) if p else []
        d = next((g for g in (m.group("d1"), m.group("d2")) if g), None)
        if d:
            regions = [DEMONYMS.get(d.lower().strip(), "")] if DEMONYMS.get(d.lower().strip()) else []
        if regions:
            basis = "citizenship" if re.search(r"citizen|national", m.group(0), re.I) else "residence"
            return "stated", {"scope": "restricted", "regions": regions, "basis": basis,
                              "korea_resident_eligible": "KR" in regions}, m
        return None
    if ELIG_CUE_KO.search(text):
        m = REGION_KR_KO.search(text)
        if m:
            regions = ["KR"]
            if REGION_OVERSEAS_KO.search(text):
                regions.append("overseas_korean")
            basis = "residence" if re.search(r"거주|체류", m.group(0)) else "citizenship"
            return "stated", {"scope": "restricted", "regions": regions, "basis": basis, "korea_resident_eligible": True}, m
        m = re.search(r"(?:자격|대상)\s*[:：|]?\s*[^.\n]{0,40}" + REGION_FOREIGN_KO.pattern, text)
        if m and not re.search(r"내국인|한국인|대한민국\s*국민", text):
            return "stated", {"scope": "restricted", "regions": ["non-KR"], "basis": "citizenship", "korea_resident_eligible": None}, m
    return None


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

# Pitchboard tracks writing/criticism calls only. Arts-council boards mix them with other notices, e.g. a venue rental
# ("대관 공고"), a dance residency, staff hiring or a school admission guide. These are not writing calls:
#  - NONWRITING_TYPE: notice types that are never a writing call, even when a literary word appears;
#  - NONWRITING_DISC: other disciplines and events (forum, screening), which are overridden by any writing cue (희곡/극본 = playwriting, 미술 비평 = criticism).
NONWRITING_TYPE = re.compile(r"(대관|채용|입찰|용역|입학|신입생|신입\s*회원|회원\s*모집|(?:특별|일반|서류|면접|실기)\s*전형|공모\s*직위|임용|venue (hire|rental)|job (opening|vacanc)|we(?:'re| are) hiring)", re.I)
NONWRITING_DISC = re.compile(r"(행사\s*(?:안내|소개)|포럼|세미나|심포지엄|토론회|설명회|공연\s*실황|상영|무용|댄스|발레|연극|뮤지컬|오페라|공연장|미술|시각\s*예술|공예|음악|국악|전시|영화|\bdance\b|tanz|탄츠|\bballet\b|\btheat(?:re|er)\b|\bvisual arts?\b|\bmusic(?:al)?\b|\bexhibition\b|\bfilm\b)", re.I)
# Scholarships may be literary awards: a writing title or writing-submission segment overrides this gate.
# Generic award/prize words alone are deliberately absent.
NONWRITING_FINANCE = re.compile(r"\b(scholarships?|bursar(?:y|ies)|entrance[- ]awards?|financial[- ]aid|tuition|fees[- ]and[- ]finances)\b", re.I)
WRITING_CUE = re.compile(r"(문학|문예|소설|시인|시집|시조|동시|수필|에세이|평론|비평|서평|리뷰|희곡|극본|대본|시나리오|동화|원고|번역|글쓰기|웹진|"
                         r"\bliter(?:ary|ature)|\bfiction|\bpoe(?:m|ms|try|t|ts)\b|\bessays?\b|\bwrit(?:ing|ers?)\b|\bmanuscripts?\b|\btranslat|\bcriticism|\bplaywrit|\bscripts?\b|\bscreenplays?\b|\breviews?\b)", re.I)


def non_writing(text: str, writing_context: str = "") -> bool:
    """True when a notice/deadline text is about something other than a writing call (rules only)."""
    return bool(NONWRITING_TYPE.search(text)
                or (NONWRITING_DISC.search(text) and not WRITING_CUE.search(text))
                or (NONWRITING_FINANCE.search(text) and not WRITING_CUE.search(text + " " + writing_context)))


def writing_submission_context(doc: Doc) -> str:
    """Finance pages have writing courses/reviews in navigation; require submission language in the same segment."""
    return " ".join([doc.title, doc.meta.get("og:title", "")] +
                    [s.text for s in doc.segments if WRITING_CUE.search(s.text)
                     and re.search(r"\b(submit|submissions?|entries|manuscripts?)\b|원고|투고|응모", s.text, re.I)])


# Board/card lists put the notice title on its own line and the "접수 : 2026-10-27~2026-11-26" line a few lines below,
# with no heading in between (e.g. "토론회 목록위원회 책토론회" / "신청 : 2/25" / "주관 ... 일시 ... 장소" / "접수 : ...").
# The row title is the only place that says what the deadline is for, so the non-writing gate reads it too.
ROW_STATUS = re.compile(r"^\s*(접수\s*(?:중|예정|마감|종료)|모집\s*(?:중|예정|마감|종료)|진행\s*중|마감|종료|open|closed)\s*$", re.I)
ROW_CONTEXT_SEGS = 3
ROW_LINE_MAX = 100


def row_context(segments, i: int) -> str:
    """Short lines directly above segment i in the same board row: walk back at most ROW_CONTEXT_SEGS lines, stopping at
    the previous row's status word or deadline line, a heading, or any long (prose) line."""
    out = []
    for j in range(i - 1, max(-1, i - 1 - ROW_CONTEXT_SEGS), -1):
        s = segments[j]
        t = s.text.strip()
        if s.kind == "h" or len(t) > ROW_LINE_MAX or ROW_STATUS.match(t) or DEADLINE_CUE.search(t):
            break
        out.append(t)
    return " ".join(reversed(out))


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
    writing_context = writing_submission_context(doc)
    page_nonwriting = non_writing(" ".join([doc.title, doc.meta.get("og:title", "")]), writing_context)

    def add(field, state, value, seg, m, conf=1.0):
        out.append(Ext(field, state, value, window(seg.text, m.start(), m.end()), dict(seg.locator, seg=seg.idx, kind=seg.kind, heading=seg.heading[:80]), conf * src_conf))

    for si, seg in enumerate(doc.segments):
        t = seg.text
        # --- dates
        hits = find_dates(t)
        if hits and DEADLINE_CUE.search(t) and not (NOT_DEADLINE_CUE.search(t) and not re.search(r"deadline|마감|접수|submit|due", t, re.I)) \
                and not page_nonwriting \
                and not non_writing(t + " " + seg.heading, writing_context) \
                and not (len(t) <= ROW_LINE_MAX and non_writing(row_context(doc.segments, si) + " " + t, writing_context)):
            # e.g. a dance-residency board row, '마감일' under an event/hiring notice heading, or a short '접수 :' line under a
            # membership-drive / book-discussion sign-up row title
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
        rv = None if m else region_value(t + (" " + seg.heading if seg.heading and not ELIG_CUE_KO.search(t) else ""))
        if m:
            add("eligibility.region", "explicitly_none", {"scope": "worldwide"}, seg, m)
        elif rv and rv[2].start() < len(t):
            add("eligibility.region", rv[0], rv[1], seg, rv[2], 0.9)
        else:
            m = ELIG_RE.search(t)
            if m:
                add("eligibility.statement", "stated", {"text": window(t, m.start(), m.end(), 200)}, seg, m, 0.7)
        # --- status
        for rx, st in ((CLOSED_RE, "closed"), (OPEN_RE, "open"), (STANDING_RE, "standing"), (None, "cancelled")):
            m = rx.search(t) if rx is not None else status_cancel_match(t)
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
    page_nonwriting = non_writing(" ".join([doc.title, doc.meta.get("og:title", "")]), writing_submission_context(doc))
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
    # Promotion also accepts rules-only pages. Always emit a blocking recruitment status for a
    # non-writing title, including when results cues suppressed recruitment or no deadline survived.
    if page_nonwriting and "recruitment" not in found:
        found["recruitment"] = ("inference", ["recruitment:non_writing_notice"])
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
            elif page_nonwriting:
                status = "not_writing_call"          # venue rental / hiring / admissions / other-discipline notice
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
