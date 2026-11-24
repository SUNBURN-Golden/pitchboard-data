"""Pure same-attempt comparisons; review flags never promote facts or call a model."""
from decimal import Decimal

from .common import digest, dumps, loads
from ...model import EMAIL_RE, PHONE_RE

PREDICATES = {"deadline": "deadline.closes_on", "pay": "compensation.rate",
              "submission_fee": "fee.submission", "eligibility": "eligibility.statement", "open_call": "status.declared",
              "simultaneous": "rights.simultaneous"}


def clean(value):
    return loads(PHONE_RE.sub("[phone]", EMAIL_RE.sub("[email]", dumps(value))))


def decimal(value):
    return str(Decimal(str(value)).normalize()) if value is not None else None


def monetary(value):
    return tuple(decimal(value.get(k)) if k.startswith("amount_") else value.get(k)
                 for k in ("amount_min", "amount_max", "currency", "unit"))


def reviews(record):
    if record.get("outcome") != "ok":
        return []
    rules, fields = record["baseline"], record["fields"]
    extensions = rules["extensions"]
    result = []
    for field, predicate in PREDICATES.items():
        accepted = fields.get(field)
        if accepted is None:
            continue
        value = accepted["value"]
        exts = [e for e in extensions if e["field"] == predicate or
                (field == "eligibility" and e["field"] == "eligibility.region")]
        reasons, second = [], False
        if field == "open_call":
            labels = rules["labels"]
            explicit_negative = any(l["status"] in ("not_writing_call", "results_not_call", "past_deadline", "stale_prior_year") for l in labels)
            declared = {e["value"].get("declared") for e in exts}
            negative_open = explicit_negative or bool(declared & {"closed", "cancelled"}) or bool(rules["deadline"] and max(rules["deadline"]) < record["as_of"])
            llm_open = value["status"] == "open"
            if ((rules["current_open"] and value["status"] in ("closed", "cancelled", "not_call")) or
                (rules["real_call"] and not value["is_real_call"]) or
                (llm_open and negative_open) or (value["is_real_call"] and not rules["real_call"] and explicit_negative)):
                reasons, second = ["call_disagreement"], True
            elif not rules["real_call"] and not explicit_negative:
                reasons = ["baseline_uncertain"]
            rule_values = {"real_call": rules["real_call"], "current_open": rules["current_open"],
                           "deadline": rules["deadline"], "declared": sorted(x for x in declared if x)}
        elif not exts:
            reasons = ["candidate_fill"]
            rule_values = []
        elif field == "deadline":
            values = sorted({e["value"]["date"] for e in exts})
            rule_values = values
            if value["date"] is None:
                reasons = ["partial_value"]
            elif values != [value["date"]]:
                reasons = ["ambiguous_rules" if len(values) > 1 else "field_conflict"]
        elif field == "simultaneous":
            states = {e["value"].get("state") for e in exts}
            rule_values = [e["value"] for e in exts]
            if len(states) > 1:
                reasons = ["ambiguous_rules"]
            elif states and value["state"] not in states:
                reasons = ["field_conflict"]
        elif field in ("pay", "submission_fee"):
            values = {monetary(e["value"]) for e in exts if e["value_state"] == "stated"}
            states = {e["value_state"] for e in exts}
            rule_values = [{"state": e["value_state"], "value": e["value"]} for e in exts]
            no_fee = value["state"] in ("unpaid", "explicitly_none")
            if (no_fee and "stated" in states) or (not no_fee and "explicitly_none" in states):
                reasons = ["field_conflict"]
            elif len(values) > 1 or len(states) > 1:
                reasons = ["ambiguous_rules"]
            elif values and monetary(value) not in values:
                signature = monetary(value)
                # Only components explicitly known by both opinions can conflict.
                paired = next(iter(values))
                reasons = ["field_conflict" if any(a != b for a, b in zip(signature, paired) if a is not None and b is not None) else "partial_value"]
        else:
            rule_values = [e["value"] for e in exts]
            # Free-text differences are incomparable scope, not proof of conflict.
            statements = {e["value"].get("text") for e in exts if e["field"] == "eligibility.statement"}
            if value["text"] not in statements:
                from ...profiles import pitchboard as prof
                from ..parse import Doc, Segment
                projected = prof.extract(Doc("html", segments=[Segment(0, "p", accepted["quote"])]))
                rule_regions = {dumps(e["value"]) for e in exts if e["field"] == "eligibility.region"}
                llm_regions = {dumps(e.value) for e in projected if e.field == "eligibility.region"}
                reasons = ([] if rule_regions and rule_regions == llm_regions else
                           ["field_conflict"] if rule_regions and llm_regions else ["scope_incomparable"])
        if not reasons:
            continue
        if record.get("source_changed"):
            reasons.append("source_changed")
        row = {"schema": "pb-shadow-review-1", "attempt_id": record["attempt_id"], "predicate": predicate,
               "requested_url": record["requested_url"], "final_url": record["final_url"],
               "body_sha256": record["body_sha256"], "text_sha256": record["text_sha256"],
               "as_of": record["as_of"], "extraction_schema": record["schema"], "rules_values": rule_values,
               "rules_quotes": [e["excerpt"][:280] for e in exts], "llm": accepted,
               "reasons": reasons, "second_opinion_required": second,
               "gap_ids": [t["gap_id"] for t in (record.get("gap_target") or {}).get("targets", []) if t["predicate"] == predicate]}
        row["review_id"] = digest(dumps([record["attempt_id"], predicate, reasons, record["schema"]]))
        result.append(clean(row))
    return result
