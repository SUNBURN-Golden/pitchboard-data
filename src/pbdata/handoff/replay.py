"""Replay successful observations through the service's rules pipeline."""
from contextlib import closing, contextmanager
import datetime as dt
import json
import os
import re
import tempfile
from urllib.parse import urlsplit

from pbdata import db, facts, timeutil
from pbdata.collect import promote, store
from pbdata.collect.executor import EXECUTOR_VERSION
from pbdata.collect.parse import Doc, Segment
from pbdata.collect.net import junk_page_url
from pbdata.collect.shadow import policy, schema
from pbdata.collect.shadow.review import PREDICATES, monetary
from pbdata.daemon import project
from pbdata.ids import sha256_hex
from pbdata.profiles import pitchboard as prof

from . import batch, status
from .bot import redact

BACKUP_PREFIX = "pitchboard_kg.pre-qwen-handoff-"
BACKUP_NAME = re.compile(BACKUP_PREFIX + r"[0-9]{8}T[0-9]{6}\.sqlite3")
SHA256 = re.compile(r"[0-9a-f]{64}")
PERMISSIONS = ("allowed_public", "allowed_conditional")


class InvalidItem(ValueError):
    pass


def backup(root, now):
    folder = root / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        target = folder / (BACKUP_PREFIX + now.strftime("%Y%m%dT%H%M%S") + ".sqlite3")
        if not target.exists():
            break
        now += dt.timedelta(seconds=1)
    fd, name = tempfile.mkstemp(dir=folder, prefix="." + target.name + ".")
    os.close(fd)
    temp = target.with_name(os.path.basename(name))
    try:
        batch.copy_database(batch.service_path(root), temp)
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        temp.replace(target)
        status.sync_dir(folder)
    finally:
        temp.unlink(missing_ok=True)
    backups = sorted(path for path in folder.iterdir() if BACKUP_NAME.fullmatch(path.name) and path.is_file())
    for old in backups[:-14]:
        old.unlink()
    status.sync_dir(folder)
    return target


def decode_items(data):
    try:
        items = [json.loads(line) for line in data.split(b"\n") if line]
        if any(not isinstance(item, dict) or item.get("outcome") not in batch.OUTCOMES
               or not isinstance(item.get("source_id"), str) or not isinstance(item.get("outlet_id"), str) for item in items):
            raise InvalidItem()
        return items
    except (ValueError, TypeError):
        raise InvalidItem() from None


def replay_doc(item):
    try:
        text = item["page_text"]
        if not isinstance(text, str) or not text.strip():
            raise InvalidItem()
        lines = text.split("\n")
        kinds = item["segment_kinds"]
        headings = item["segment_headings"]
        if (not isinstance(kinds, list) or not isinstance(headings, list) or len(lines) != len(kinds)
                or len(lines) != len(headings) or any(kind not in ("h", "p", "li", "row", "line") for kind in kinds)):
            raise InvalidItem()
        segments = [Segment(i, kind, redact(line), redact(heading))
                    for i, (line, kind, heading) in enumerate(zip(lines, kinds, headings))]
        return Doc("html", title=redact(item["title"]), segments=segments, meta={"og:title": redact(item["og_title"])})
    except (KeyError, TypeError, ValueError):
        raise InvalidItem() from None


def agreements(item, verified, call_statuses):
    qwen = item.get("qwen")
    if not isinstance(qwen, dict) or set(qwen) != set(schema.FIELDS):
        raise InvalidItem()
    result = {}
    for field, predicate in PREDICATES.items():
        proposal = qwen[field]
        if proposal is not None and (not isinstance(proposal, dict) or not schema.valid_value(field, proposal.get("value"))
                                     or not isinstance(proposal.get("quote"), str)):
            raise InvalidItem()
        rows = [row for row in verified if row["predicate"] == predicate
                or (field == "eligibility" and row["predicate"] == "eligibility.region")]
        values = [json.loads(row["value_json"]) for row in rows]
        states = {row["value_state"] for row in rows}
        statuses = set()
        if field == "open_call":
            statuses = {value.get("declared") for value in values}
            statuses.update({"announced": "upcoming", "extended": "open"}.get(s, s) for s in call_statuses)
        match = False
        if proposal is not None:
            value = proposal["value"]
            if field == "open_call":
                match = value["is_real_call"] and value["status"] in statuses
            elif field == "deadline":
                match = value["date"] is not None and any(value["date"] == rule["date"] for rule in values)
            elif field in ("pay", "submission_fee"):
                if value["state"] in ("unpaid", "explicitly_none"):
                    match = "explicitly_none" in states
                elif value["state"] in ("paid", "stated", "conditional"):
                    match = any(row["value_state"] == "stated" and monetary(value) == monetary(rule)
                                for row, rule in zip(rows, values))
            else:
                quote_doc = Doc("html", segments=[Segment(0, "p", proposal["quote"])])
                regions = [ext.value for ext in prof.extract(quote_doc) if ext.field == "eligibility.region"]
                match = any(value["text"] == rule.get("text") or rule in regions for rule in values)
        categories = []
        if match:
            categories.append("agree")
        if proposal is not None and not match:
            categories.append("qwen_only")
        if (rows or statuses) and not match:
            categories.append("rules_only")
        if categories:
            result[field] = categories
    return result


def replay_item(svc, batch_id, item, now):
    detail = {field: item[field] for field in ("source_id", "outlet_id", "outcome")}
    if item["outcome"] != "ok":
        return detail

    def rejected(reason):
        return dict(detail, rejection=reason, agreements=agreements(item, [], ()))

    try:
        fetched_at = timeutil.parse_iso(item["fetched_at"])
        http_status = item["http_status"]
        if (type(http_status) is not int or not 200 <= http_status < 300 or
                not SHA256.fullmatch(item["content_sha256"]) or not SHA256.fullmatch(item["source_content_hash"]) or
                type(item["bytes_len"]) is not int or item["bytes_len"] <= 0 or
                type(item["text_truncated"]) is not bool or type(item["partial_segment"]) is not bool or
                item["content_type"] not in ("text/html", "application/xhtml+xml")):
            raise InvalidItem()
        requested = policy.canonical_url(item["url"])
        final_url = policy.canonical_url(item["final_url"])
    except policy.Stop:
        return rejected("blocked_url")
    except (KeyError, TypeError, ValueError):
        raise InvalidItem() from None
    if fetched_at > timeutil.parse_iso(now):
        raise InvalidItem()
    source = svc.execute("SELECT * FROM pb_source WHERE source_id=?", (item["source_id"],)).fetchone()
    if (source is None or source["owner_outlet_id"] != item["outlet_id"] or source["retired_reason"] is not None
            or source["permission"] not in PERMISSIONS or source["canonical_url"] != item["url"] or junk_page_url(item["url"])):
        return rejected("source_unavailable")
    requested_host, final_host = (urlsplit(url).hostname.removeprefix("www.") for url in (requested, final_url))
    if requested_host != final_host and not svc.execute(
        "SELECT 1 FROM pb_source WHERE canonical_url=? AND owner_outlet_id=? AND authority='official_primary' "
        "AND retired_reason IS NULL AND permission IN ('allowed_public','allowed_conditional')",
        (final_url, item["outlet_id"]),
    ).fetchone():
        return rejected("redirect_not_official")
    previous = store.last_raw(svc, item["url"], active_source=True)
    if previous and previous["fetched_at"] > item["fetched_at"]:
        return rejected("newer_raw_exists")

    doc = replay_doc(item)
    stored_source_id = store.ensure_page_source(svc, item["url"], item["source_id"], now)
    if stored_source_id != source["source_id"]:
        raise InvalidItem()
    seg_hashes = [store.seg_hash(segment.text) for segment in doc.segments]
    text_hash = sha256_hex("\n".join(" ".join(segment.text.split()) for segment in doc.segments))
    change = "new" if previous is None else ("unchanged" if previous["text_hash"] == text_hash else "changed")
    raw_id = store.insert_raw(
        svc, now, source_id=stored_source_id, url=item["url"], final_url=final_url, kind="html", fetch_method="html",
        content_type=item["content_type"], bytes_len=item["bytes_len"], content_hash=item["source_content_hash"],
        http_status=http_status, outcome="ok", extractor_version=EXECUTOR_VERSION, run_id=batch_id,
        fetched_at=item["fetched_at"], prev_raw_id=previous["raw_id"] if previous else None, change_state=change,
        text_hash=text_hash, seg_hashes=json.dumps(seg_hashes[:2000]), segment_count=len(seg_hashes), depth=0,
    )
    store.ensure_revision(svc, stored_source_id, text_hash, seg_hashes, item["fetched_at"], EXECUTOR_VERSION)
    official = source["authority"] == "official_primary"
    stats = promote.PromoteStats()
    if not item["partial_segment"]:
        exts = prof.extract(doc)
        labels = prof.label_doc(doc, exts, timeutil.parse_iso(now).astimezone(timeutil.KST).date(), source_official=official)
        store.put_extractions(svc, raw_id, exts, "rules", prof.EXTRACTOR_VERSION, now)
        store.put_labels(svc, raw_id, labels, prof.LABELER_VERSION, now)
        stats = promote.promote_raw(svc, raw_id, now, verified_only=True, complete=not item["text_truncated"])
    else:
        stats.skipped_label = "partial_segment"

    verified = []
    for fact_id in sorted(set(stats.observed_fact_ids)):
        row = svc.execute("SELECT * FROM pb_fact WHERE fact_id=? AND epistemic='verified_fact' AND superseded_at IS NULL", (fact_id,)).fetchone()
        if row:
            verified.append(row)
    call_statuses = set()
    for call_id in {row["subject_id"] for row in verified if row["subject_type"] == "call"}:
        call_statuses.add(svc.execute("SELECT status FROM pb_call_edition WHERE call_id=?", (call_id,)).fetchone()[0])
    detail.update(raw_id=raw_id, facts_promoted=sum(row["fact_id"] in stats.fact_ids for row in verified),
                  calls_created=stats.calls_created, skipped_label=stats.skipped_label, rejections=stats.rejected,
                  fact_ids=[row["fact_id"] for row in verified], agreements=agreements(item, verified, call_statuses))
    if official and not stats.rejected and stats.skipped_label not in ("permission", "source_ceiling", "partial_segment"):
        detail.update(accepted=True, url=item["url"], final_url=final_url,
                      checked_on=timeutil.parse_iso(now).astimezone(timeutil.KST).date().isoformat())
    return detail


def open_ledger(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS batches(
        batch_id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('processed','failed')),
        started_at TEXT NOT NULL, finished_at TEXT NOT NULL, detail JSON NOT NULL
    )""")
    return conn


@contextmanager
def read_ledger(path):
    if not path.is_file():
        yield None
        return
    with closing(batch.readonly(path)) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='batches'").fetchone()
        yield conn if exists else None


def pending(folder, ledger, limit, *, recover=False):
    queued = []
    for path, data in batch.ready_batches(folder / "inbox"):
        prior = ledger.execute("SELECT * FROM batches WHERE batch_id=?", (path.name,)).fetchone() if ledger else None
        if prior:
            if recover:
                close_batch(folder, path, data, prior["state"], json.loads(prior["detail"]))
        elif len(queued) < limit:
            queued.append((path, data))
            if len(queued) == limit:
                break
    return queued


def close_batch(folder, path, data, state, detail):
    try:
        # Advisory model values only appear in filesystem artifacts.
        review = dict(detail, batch_id=path.name, state=state)
        if state == "processed":
            review["items"] = [dict(row, qwen=item.get("qwen")) for row, item in zip(detail["items"], decode_items(data))]
        status.atomic_json(folder / "review" / (path.name + ".json"), review)
        destination = folder / state
        destination.mkdir(parents=True, exist_ok=True)
        path.rename(destination / path.name)
        status.sync_dir(destination)
        status.sync_dir(path.parent)
    except (OSError, InvalidItem):
        print("handoff batch artifact_error", flush=True)


def apply_batch(svc, path, data, started_at):
    with db.transaction(svc):
        # A service-side receipt closes the crash window before the sidecar
        # ledger commit without changing the service database's WAL mode.
        svc.execute("CREATE TABLE IF NOT EXISTS pb_qwen_handoff_receipt(batch_id TEXT PRIMARY KEY, detail TEXT NOT NULL)")
        receipt = svc.execute("SELECT detail FROM pb_qwen_handoff_receipt WHERE batch_id=?", (path.name,)).fetchone()
        if receipt:
            return json.loads(receipt["detail"])
        items = [replay_item(svc, path.name, item, started_at) for item in decode_items(data)]
        detail = {"items": items, "items_ok": sum("raw_id" in item for item in items),
                  "facts_promoted": sum(item.get("facts_promoted", 0) for item in items),
                  "calls_created": sum(item.get("calls_created", 0) for item in items)}
        if detail["items_ok"]:
            facts.resolve_noncontradicting_conflicts(svc, started_at)
            project.recompute(svc, started_at)
        svc.execute("INSERT INTO pb_qwen_handoff_receipt VALUES(?,?)", (path.name, status.json_line(detail).decode()))
        return detail


def process_batches(root, folder, queued, record, clock):
    if not queued:
        return
    try:
        record["backup_path"] = str(backup(root, clock()))
    except Exception:
        record["db_error"] = "backup_failed"
        return
    with closing(open_ledger(folder / "ledger.sqlite3")) as ledger, closing(db.connect(batch.service_path(root))) as svc:
        for path, data in queued:
            started_at = timeutil.iso(clock())
            try:
                detail = apply_batch(svc, path, data, started_at)
                state = "processed"
            except Exception as exc:
                if svc.in_transaction:
                    svc.rollback()
                detail = {"error_code": "invalid_item" if isinstance(exc, InvalidItem) else "apply_error"}
                state = "failed"
            ledger.execute("INSERT INTO batches VALUES(?,?,?,?,?)",
                           (path.name, state, started_at, timeutil.iso(clock()), status.json_line(detail).decode()))
            if state == "processed":
                record["batches_consumed"] += 1
                for field in ("items_ok", "facts_promoted", "calls_created"):
                    record[field] += detail[field]
            else:
                record["batches_failed"] += 1
            close_batch(folder, path, data, state, detail)


def export_projection(svc, root, now):
    rows = [json.loads(row[0]) for row in svc.execute("SELECT payload FROM pb_search_projection ORDER BY call_id")]
    status.atomic_json(root / "exports/projection_latest.json", {"as_of": now, "calls": rows})
