"""Gold-sample evaluation of the rules-only extractor/labeler (hand-labelled synthetic snippets, tests/gold/)."""
from __future__ import annotations

import datetime as dt
import html
import json
from typing import Dict, List

from ..profiles import pitchboard as prof
from . import parse


def _doc(item: dict):
    return parse.parse_html(("<html lang='%s'><head><title>%s</title></head><body><h1>%s</h1><p>%s</p></body></html>" %
                            (item["lang"], html.escape(item["title"]), html.escape(item["title"]), html.escape(item["text"]))).encode("utf-8"), "https://example.org/x")


def run_item(item: dict, today: dt.date) -> dict:
    doc = _doc(item)
    exts = prof.extract(doc)
    labels = prof.label_doc(doc, exts, today)
    by = {}
    for e in exts:
        by.setdefault(e.field, []).append(e.value)
    got = {"labels": sorted({l.label for l in labels}), "eligible": any(l.open_call_eligible for l in labels),
           "status": sorted({l.status for l in labels if l.label == "recruitment"}),
           "deadlines": sorted({v["date"] for v in by.get("deadline.closes_on", []) if v}),
           "fee": [v.get("fee") or v.get("amount_min") for v in by.get("fee.submission", [])],
           "pay": [v.get("amount_min") for v in by.get("compensation.rate", []) if v.get("amount_min") is not None] or [v.get("pay") for v in by.get("compensation.rate", [])],
           "simul": [v.get("state") for v in by.get("rights.simultaneous", [])],
           "ai": [v.get("state") for v in by.get("policy.ai", [])],
           "region": [v.get("scope") for v in by.get("eligibility.region", [])],
           "elig_stmt": bool(by.get("eligibility.statement"))}
    return got


def evaluate(path: str) -> Dict:
    data = json.load(open(path))
    today = dt.date.fromisoformat(data["today"])
    tot = {"label": [0, 0], "eligible": [0, 0], "status": [0, 0], "deadline": [0, 0], "fee": [0, 0], "pay": [0, 0], "simul": [0, 0], "ai": [0, 0], "region": [0, 0], "eligibility": [0, 0]}
    errors: List[dict] = []

    def chk(k, ok, item, got):
        tot[k][1] += 1
        tot[k][0] += 1 if ok else 0
        if not ok:
            errors.append({"id": item["id"], "field": k, "expect": item["expect"].get(k if k != "label" else "label"), "got": got})

    for item in data["items"]:
        e, got = item["expect"], run_item(item, today)
        want = e["label"]
        chk("label", (want in got["labels"]) if want != "other" else got["labels"] == ["other"], item, got["labels"])
        if "eligible" in e:
            chk("eligible", got["eligible"] == e["eligible"], item, got["eligible"])
        if "status" in e:
            chk("status", e["status"] in got["status"], item, got["status"])
        if "deadline" in e:
            d = e["deadline"]
            chk("deadline", (d in got["deadlines"]) if d else not got["deadlines"], item, got["deadlines"])
        if "fee" in e:
            f = e["fee"]
            chk("fee", (f in got["fee"]) if f == "none" else any(isinstance(x, (int, float)) and abs(x - f) < 1e-6 for x in got["fee"]), item, got["fee"])
        if "pay" in e:
            chk("pay", any(isinstance(x, (int, float)) and abs(x - e["pay"]) < 1e-6 for x in got["pay"]), item, got["pay"])
        if "simul" in e:
            chk("simul", e["simul"] in got["simul"], item, got["simul"])
        if "ai" in e:
            chk("ai", e["ai"] in got["ai"], item, got["ai"])
        if "region" in e:
            chk("region", e["region"] in got["region"], item, got["region"])
        if "eligibility" in e:
            chk("eligibility", got["elig_stmt"], item, got["elig_stmt"])
    acc = {k: {"correct": v[0], "total": v[1], "accuracy": round(v[0] / v[1], 3) if v[1] else None} for k, v in tot.items() if v[1]}
    allc = sum(v[0] for v in tot.values())
    allt = sum(v[1] for v in tot.values())
    # false positive: an item expected NOT eligible but predicted eligible (the costly error)
    return {"items": len(data["items"]), "checks": allt, "correct": allc, "accuracy": round(allc / allt, 3), "by_field": acc, "errors": errors}


def entity_eval(path: str) -> Dict:
    from .resolve import same_entity
    pairs = json.load(open(path))["pairs"]
    fm = mm = neg = pos = 0
    detail = []
    for a, ha, b, hb, same in pairs:
        pred = same_entity(a, ha, b, hb)
        neg += 0 if same else 1
        pos += 1 if same else 0
        if pred and not same:
            fm += 1
            detail.append({"false_merge": [a, b]})
        if same and not pred:
            mm += 1
            detail.append({"missed_merge": [a, b]})
    return {"pairs": len(pairs), "negatives": neg, "false_merges": fm, "false_merge_rate": round(fm / neg, 3) if neg else None,
            "positives": pos, "missed_merges": mm, "detail": detail}
