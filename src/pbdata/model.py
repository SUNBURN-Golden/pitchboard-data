"""Vocabulary and value objects for the fact model (docs/DESIGN.md section 2)."""
from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, Dict, List, Optional

EPISTEMIC = ("verified_fact", "source_claim", "ai_inference", "unverified")
EPISTEMIC_RANK = {"verified_fact": 3, "source_claim": 2, "ai_inference": 1, "unverified": 0}
VALUE_STATES = ("stated", "explicitly_none", "not_stated_in_inspected_scope", "not_inspected")
AUTHORITIES = ("official_primary", "attributed_secondary", "discovery_lead", "unknown")
SUBJECT_TYPES = ("outlet", "organizer", "call", "source")

CALL_STATUSES = ("candidate", "announced", "open", "extended", "standing", "closed", "cancelled",
                 "status_unknown", "superseded", "archived")
OPEN_LIKE = ("open", "extended", "standing")

# Predicates the ontology knows. Anything else is rejected (controlled vocabulary).
PREDICATES = {
    "status.declared": "open|closed|cancelled as declared by the source",
    "deadline.opens_on": "YYYY-MM-DD window start",
    "deadline.closes_on": "YYYY-MM-DD window end (+ tz info)",
    "fee.submission": "submission/reading/entry fee",
    "compensation.rate": "pay terms",
    "rights.simultaneous": "simultaneous submissions allowed or not",
    "policy.ai": "AI-generated work policy",
    "eligibility.region": "who may apply by region",
    "eligibility.statement": "free-text eligibility statement (not structured)",
    "rights.statement": "free-text rights statement (not structured)",
    "scope.genre": "genre accepted (backfill)",
    "scope.submission_category": "category accepted (backfill)",
    "channel.url": "where to submit",
}
# Predicates whose absence we actively look for on a full page (see inspected-scope facts).
CORE_TERMS = ("status.declared", "deadline.closes_on", "fee.submission", "compensation.rate",
              "rights.simultaneous", "policy.ai", "eligibility.region")

# Freshness windows (days) per predicate family; after this a verified fact needs re-checking (V-12).
FRESH_DAYS = {"status.": 7, "deadline.": 7, "fee.": 90, "compensation.": 90, "rights.": 90,
              "policy.": 90, "eligibility.": 90, "scope.": 90, "channel.": 90}

# Statements that justify value_state='explicitly_none' (V-03), per predicate.
EXPLICIT_NONE_PATTERNS = {
    "fee.submission": re.compile(r"\b(no (submission|reading|entry) fees?|(submissions?|submitting) (is|are) free|free to submit|"
                                 r"does not charge|do not charge)\b|(응모료|참가비|접수비|참가료|심사비|응모비)\s*(은|는)?\s*(없|무료|면제)|(응모|참가|접수)\s*무료", re.I),
    "compensation.rate": re.compile(r"\b(we do not pay|unpaid|no payment|does not pay|do not pay)\b|원고료\s*(은|는)?\s*(없|지급하지)", re.I),
    "eligibility.region": re.compile(r"\b(open to (writers|submissions|everyone) (from )?(everywhere|worldwide|around the world|"
                                     r"internationally)|international submissions are welcome|writers worldwide)\b", re.I),
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[ -]?)?(?:\(?\d{2,4}\)?[ -]?)\d{3,4}[ -]\d{4}(?!\d)")


class ValidationError(Exception):
    def __init__(self, violations: List["Violation"]):
        self.violations = violations
        super().__init__("; ".join("%s: %s" % (v.code, v.message) for v in violations))


@dataclasses.dataclass(frozen=True)
class Violation:
    code: str
    message: str


def freshness_days(predicate: str) -> int:
    for prefix, days in FRESH_DAYS.items():
        if predicate.startswith(prefix):
            return days
    return 30


def normalize_money(value: Dict[str, Any]) -> Dict[str, Any]:
    """Add 'comparable': True only when currency AND unit AND a numeric amount are known (V-05)."""
    out = dict(value)
    has_amount = out.get("amount_min") is not None or out.get("amount_max") is not None
    out["comparable"] = bool(has_amount and out.get("currency") and out.get("unit"))
    return out


def dumps(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclasses.dataclass
class FactInput:
    subject_type: str
    subject_id: str
    predicate: str
    source_id: str
    value_state: str
    epistemic: str
    collected_at: str
    extractor: str
    extractor_version: str
    scope_ref: str = "unspecified"
    value: Optional[Dict[str, Any]] = None
    value_raw: Optional[str] = None
    evidence_id: Optional[str] = None
    inspected_revision_id: Optional[str] = None
    source_authority: str = "unknown"
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    verified_at: Optional[str] = None
    verifier: Optional[str] = None
    fresh_until: Optional[str] = None
    correction_of: Optional[str] = None
    correction_reason: Optional[str] = None
    run_id: Optional[str] = None
    retain_until: Optional[str] = None
