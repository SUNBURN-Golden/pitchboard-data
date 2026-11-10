"""Conservatively retract old open-call classifications using retained evidence, never new collection.

Raw bodies are not retained. Absence of a reproduced deadline/eligible label in the
partial excerpts is NOT a rejection: every stated backing fact must instead hit
the current explicit non-writing gate, or have a current full-page rejection for
its source/content. A missing, changed or ambiguous basis keeps the edition. Facts
and labels remain untouched; a status correction records the audit.
"""
from __future__ import annotations

import datetime as dt
import json

from .. import statemachine
from ..db import transaction
from ..ids import sha256_hex
from ..profiles import pitchboard as prof
from .parse import Doc, Segment
from .promote import CALL_PREDICATES

ACTIVE_STATUSES = ("announced", "open", "extended", "standing")


def _source_evidence(svc, source_id):
    raw = svc.execute("SELECT * FROM pb_raw_ref WHERE source_id=? AND outcome='ok' "
                      "ORDER BY fetched_at DESC, rowid DESC LIMIT 1", (source_id,)).fetchone()
    if raw is None or not raw["text_hash"] or raw["kind"] not in ("html", "pdf"):
        return None
    rev = svc.execute("SELECT revision_id FROM pb_source_revision WHERE source_id=? AND content_hash=?",
                      (source_id, raw["text_hash"])).fetchone()
    if rev is None:
        return None
    evidence = svc.execute("SELECT * FROM pb_evidence WHERE source_id=? AND revision_id=? "
                           "ORDER BY paragraph_index, evidence_id", (source_id, rev[0])).fetchall()
    if any(sha256_hex(e["excerpt"]) != e["quote_hash"] for e in evidence):
        return None
    # An unchanged fetch has no extraction rows. Recover the latest retained
    # extraction for exactly the same source/content, never a different revision.
    extracted = svc.execute("SELECT r.raw_id FROM pb_raw_ref r WHERE r.source_id=? AND r.text_hash=? "
                            "AND r.outcome='ok' AND EXISTS (SELECT 1 FROM pb_extraction x WHERE x.raw_id=r.raw_id) "
                            "ORDER BY r.fetched_at DESC, r.rowid DESC LIMIT 1", (source_id, raw["text_hash"])).fetchone()
    exts = svc.execute("SELECT * FROM pb_extraction WHERE raw_id=? ORDER BY rowid", (extracted[0],)).fetchall() if extracted else []
    if any(e["extractor"].startswith(("ocr:", "ai:")) for e in exts):
        return None
    # These snippets supply writing overrides only. Do not pretend gaps between
    # retained snippets were adjacent board rows or infer a missing page title.
    texts = list(dict.fromkeys([t for e in evidence for t in (e["excerpt"], e["heading"]) if t] +
                               [e["excerpt"] for e in exts]))
    doc = Doc(raw["kind"], segments=[Segment(i, "p", t) for i, t in enumerate(texts)])
    headings = [e["heading"] for e in evidence] + [json.loads(e["locator"]).get("heading", "") for e in exts]
    # A retained literary heading may have been the now-discarded page title.
    # It can preserve a finance-page writing override, never establish rejection.
    writing = prof.writing_submission_context(doc) + " " + " ".join(h for h in headings if prof.WRITING_CUE.search(h))
    labeled = svc.execute("SELECT r.raw_id FROM pb_raw_ref r WHERE r.source_id=? AND r.text_hash=? "
                          "AND r.outcome='ok' AND EXISTS (SELECT 1 FROM pb_label l WHERE l.raw_id=r.raw_id) "
                          "ORDER BY r.fetched_at DESC, r.rowid DESC LIMIT 1", (source_id, raw["text_hash"])).fetchone()
    # Current-version labels were computed against the complete page before its
    # body/title was discarded. They carry more context than excerpt replay.
    labels = svc.execute("SELECT * FROM pb_label WHERE raw_id=? AND labeler_version=? ORDER BY label_id",
                         (labeled[0], prof.LABELER_VERSION)).fetchall() if labeled else []
    return raw, rev[0], {e["evidence_id"]: e for e in evidence}, writing, labels


def reconcile(svc, now: str) -> dict:
    """Close only explicitly non-writing editions; never promote or infer source relations.

    `closed` is an evidence-backed correction of eligibility, not a claim that the
    source cancelled the call or that its stated deadline passed. Superseded is
    reserved for reviewed replacements/merges. The existing state machine records
    the correction and marks the edition dirty for the daemon's next projection.
    """
    changed, checked = [], 0
    today = dt.date.fromisoformat(now[:10])
    with transaction(svc):
        calls = svc.execute("SELECT call_id, status FROM pb_call_edition WHERE status IN (?,?,?,?) ORDER BY call_id",
                            ACTIVE_STATUSES).fetchall()
        cache = {}
        for call in calls:
            checked += 1
            backing = svc.execute("SELECT * FROM pb_fact WHERE subject_type='call' AND subject_id=? "
                                  "AND superseded_at IS NULL AND value_state='stated' AND predicate IN (?,?,?) ORDER BY source_id, fact_id",
                                  (call["call_id"], *sorted(CALL_PREDICATES))).fetchall()
            rejected = []
            for fact in backing:
                sid = fact["source_id"]
                if sid not in cache:
                    cache[sid] = _source_evidence(svc, sid)
                snapshot = cache[sid]
                if snapshot is None or fact["extractor"].startswith(("ocr:", "ai:")):
                    break
                raw, revision_id, evidence, writing, stored_labels = snapshot
                if any(l["open_call_eligible"] for l in stored_labels):
                    break
                page_rejected = any(l["label"] == "recruitment" and l["status"] == "not_writing_call" for l in stored_labels)
                ev = svc.execute("SELECT * FROM pb_evidence WHERE evidence_id=? AND source_id=?",
                                 (fact["evidence_id"], sid)).fetchone()
                if ev is None or sha256_hex(ev["excerpt"]) != ev["quote_hash"]:
                    break
                if page_rejected:
                    rejected.append({"source_id": sid, "raw_id": raw["raw_id"], "revision_id": revision_id,
                                     "fact_id": fact["fact_id"], "evidence_id": ev["evidence_id"],
                                     "gate": "current full-page recruitment:not_writing_call",
                                     "labels": [{"label_id": l["label_id"], "raw_id": l["raw_id"], "status": l["status"],
                                                 "rules": json.loads(l["rule_ids"])} for l in stored_labels]})
                    continue
                if ev["evidence_id"] not in evidence:  # no current page rejection or matching latest excerpt
                    break
                # Keep the actual heading/excerpt scope of this basis. Other
                # retained snippets may preserve a writing override, never reject
                # this basis merely because another notice on the page is non-writing.
                doc = Doc(raw["kind"], segments=[Segment(0, "p", ev["excerpt"], ev["heading"])])
                if not prof.non_writing(ev["excerpt"] + " " + ev["heading"], writing):
                    break
                # Run the current extractor/labeler too, with available writing
                # context. Missing labels or dates alone must never close a call.
                if writing.strip():
                    doc.segments.append(Segment(1, "p", writing))
                labels = prof.label_doc(doc, prof.extract(doc), today)
                if any(l.open_call_eligible for l in labels):
                    break
                rejected.append({"source_id": sid, "raw_id": raw["raw_id"], "revision_id": revision_id,
                                 "fact_id": fact["fact_id"], "evidence_id": ev["evidence_id"],
                                 "gate": "non_writing(excerpt+heading, writing_submission_context)",
                                 "labels": [{"label": l.label, "status": l.status, "rules": l.rules} for l in labels]})
            else:
                if not rejected:
                    continue
                reason = json.dumps({"correction": "prior open-call basis rejected by current non-writing gates",
                                     "extractor_version": prof.EXTRACTOR_VERSION, "labeler_version": prof.LABELER_VERSION,
                                     "backing": rejected}, sort_keys=True, ensure_ascii=False)
                event_id = statemachine.transition(svc, call["call_id"], "closed", statemachine.EV, now,
                                                  "rules:open_call_reconcile", reason, kind="correction",
                                                  evidence_id=rejected[0]["evidence_id"], fact_id=rejected[0]["fact_id"])
                changed.append({"call_id": call["call_id"], "from_status": call["status"],
                                "to_status": "closed", "event_id": event_id})
    return {"checked": checked, "closed_nonwriting": len(changed), "changes": changed}
