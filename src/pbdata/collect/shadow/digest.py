"""Optional deterministic private drafts; zero model calls or publication hooks."""
from .common import Stop, digest, dumps
from ...model import EMAIL_RE, PHONE_RE


def drafts(record, languages=()):
    if not isinstance(languages, (tuple, list)) or len(set(languages)) != len(languages) or set(languages) - {"ko", "en"}:
        raise Stop("invalid_digest_languages")
    if record.get("outcome") != "ok" or not languages or record.get("source_changed"):
        return []
    if any(r["second_opinion_required"] or set(r["reasons"]) - {"candidate_fill"} for r in record.get("reviews", [])):
        return []
    fields = record["fields"]
    call = fields.get("open_call")
    if not call or call["value"]["status"] != "open":
        return []
    result = []
    for language in languages:
        parts = ["글쓰기 투고 접수 중" if language == "ko" else "Writing submissions open"]
        used = ["open_call"]
        deadline = fields.get("deadline")
        if deadline and deadline["value"]["date"]:
            parts.append(("마감 " if language == "ko" else "Deadline ") + deadline["value"]["date"])
            used.append("deadline")
        fee = fields.get("submission_fee")
        if fee:
            v = fee["value"]
            if v["state"] == "explicitly_none":
                parts.append("응모료 없음" if language == "ko" else "No submission fee")
                used.append("submission_fee")
            elif v["amount_min"] == v["amount_max"] and v["amount_min"] is not None and v["currency"]:
                parts.append(("응모료 " if language == "ko" else "Submission fee ") + v["currency"] + " " + v["amount_min"])
                used.append("submission_fee")
        line = " · ".join(parts)
        if len(line) > 280 or "\n" in line or "\r" in line or EMAIL_RE.search(line) or PHONE_RE.search(line):
            continue
        result.append({"schema": "pb-shadow-digest-1", "draft_id": digest(dumps([record["attempt_id"], language, "digest-1"])),
                       "attempt_id": record["attempt_id"], "body_sha256": record["body_sha256"],
                       "text_sha256": record["text_sha256"], "as_of": record["as_of"],
                       "language": language, "line": line, "draft": True,
                       "supporting_fields": [{"field": f, "field_id": digest(dumps([record["attempt_id"], f]))} for f in used]})
    return result
