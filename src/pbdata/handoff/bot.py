"""Read-only source selection, proxy-only fetching, and Qwen batch publication."""
import argparse
from contextlib import closing, nullcontext
import dataclasses
import datetime as dt
import os
from pathlib import Path
import subprocess
import tempfile
import time

from pbdata import db, timeutil
from pbdata.collect import egress
from pbdata.collect.net import junk_page_url
from pbdata.collect.parse import Doc
from pbdata.collect.shadow import common, policy, runner, schema
from pbdata.model import EMAIL_RE, PHONE_RE

from . import batch, status

CONFIG = Path(__file__).resolve().parents[3] / "config/qwen_bot.json"
BLOCK_STATUSES = {401, 402, 403, 429, 451}
BLOCK_CODES = {"access_wall", "blocked_host", "blocked_network", "host_halted"}
FETCH_CODES = BLOCK_CODES | {"denied_robots", "invalid_robots", "denied_http", "unsupported_content", "empty_page",
                             "bad_url", "redirect_limit", "invalid_transport_body", "fetch_error"}


def probe_proxy(*, sleep=time.sleep):
    for attempt in range(5):
        try:
            host, port = egress.preflight()
            address = f"[{host}]" if ":" in host else host
            result = subprocess.run(
                ["curl", "--disable", "--silent", "--fail", "--max-time", "20", "--noproxy", "",
                 "--socks5-hostname", f"{address}:{port}", "https://example.com/"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        print(f"qwen bot proxy probe {attempt + 1}/5 failed", flush=True)
        if attempt < 4:
            sleep(60)
    return False


def select_targets(conn, now, recent):
    today = now.astimezone(timeutil.KST).date()
    cutoff = timeutil.iso(now - dt.timedelta(hours=20))
    rows = conn.execute("""
        WITH live AS (
            SELECT f.source_id,
                MIN(CASE WHEN json_extract(f.value_json,'$.date') BETWEEN ? AND ?
                         THEN json_extract(f.value_json,'$.date') END) AS urgent
            FROM pb_fact f JOIN pb_call_edition c ON f.subject_id=c.call_id
            WHERE f.subject_type='call' AND c.status IN ('candidate','announced','open','extended')
                AND f.predicate='deadline.closes_on' AND f.superseded_at IS NULL AND f.value_state='stated'
            GROUP BY f.source_id
        )
        SELECT s.source_id, s.owner_outlet_id AS outlet_id, s.canonical_url AS url,
            live.urgent, s.last_success_at,
            (SELECT MAX(r.fetched_at) FROM pb_raw_ref r WHERE r.source_id=s.source_id AND r.outcome='ok') AS raw_success
        FROM pb_source s LEFT JOIN live USING(source_id)
        WHERE (s.authority='official_primary' OR live.source_id IS NOT NULL)
            AND s.owner_outlet_id IS NOT NULL AND s.retired_reason IS NULL
            AND s.permission IN ('allowed_public','allowed_conditional')
        """, (today.isoformat(), (today + dt.timedelta(days=21)).isoformat()))
    targets = []
    for row in rows:
        url = row["url"]
        if junk_page_url(url):
            continue
        try:
            key = policy.canonical_url(url)
        except common.Stop:
            # Let PolicyFetcher record the policy rejection without a network request.
            key = url
        checked = recent.get(key, "")
        if checked >= cutoff:
            continue
        order = (row["urgent"] or "9999", max(checked, row["last_success_at"] or "", row["raw_success"] or ""), url)
        targets.append((order, key, {field: row[field] for field in ("source_id", "outlet_id", "url")}))
    seen = set()
    for _, key, target in sorted(targets):
        if key not in seen:
            seen.add(key)
            yield key, target


def redact(text, key=""):
    if key:
        text = text.replace(key, "[redacted]")
    return PHONE_RE.sub("[phone]", EMAIL_RE.sub("[email]", text))


def prepare_doc(doc, byte_cap, key):
    """Retain complete segments when possible, with the same text used for Qwen and replay."""
    segments = []
    remaining = byte_cap
    truncated = partial = False
    for segment in doc.segments:
        value = redact(segment.text, key)
        encoded = value.encode("utf-8")
        separator = bool(segments)
        if len(encoded) + separator > remaining:
            truncated = True
            if not segments:
                partial = True
                segments.append(dataclasses.replace(segment, text=encoded[:remaining].decode("utf-8", "ignore"),
                                                    heading=redact(segment.heading, key)))
            break
        segments.append(dataclasses.replace(segment, text=value, heading=redact(segment.heading, key)))
        remaining -= len(encoded) + separator
    return Doc("html", title=redact(doc.title, key), segments=segments,
               meta={"og:title": redact(doc.meta.get("og:title", ""), key)}), truncated, partial


def extract(doc, as_of, caps, key, transport, budget, item):
    if not key:
        item.update(outcome="llm_error", error_code="key_unavailable")
        return
    try:
        body = schema.request_body(doc, as_of, caps, provider="qwen")
        for repair in (False, True):
            if budget["llm_calls"] >= caps["max_llm_calls_per_run"]:
                item.update(outcome="skipped_cap", error_code="llm_cap")
                return
            if key in common.dumps(body):
                raise common.Stop("credential_in_request")
            budget["llm_calls"] += 1
            raw = transport(body, key, caps["timeout_seconds"])
            usage = raw.get("usage") if isinstance(raw, dict) else None
            if isinstance(usage, dict):
                for field in batch.TOKEN_FIELDS:
                    value = usage.get(field)
                    if type(value) is int and value >= 0:
                        item["usage"][field] += value
            if key in common.dumps(raw):
                raise common.Stop("credential_in_response")
            try:
                fields, reasons, _ = schema.response(raw, doc, as_of, caps, provider="qwen", strict_repair=repair)
            except common.Stop as exc:
                if repair or str(exc) not in runner.REPAIR_SCHEMA_CODES:
                    raise
                prior = exc.prior if isinstance(exc, schema.SchemaRepairCandidate) else None
                body = schema.repair_body(body, prior)
                continue
            if key in common.dumps(fields):
                raise common.Stop("credential_in_response")
            item.update(qwen=fields, validation_reasons=reasons, returned_model=raw["model"], outcome="ok")
            return
    except common.Stop as exc:
        code = str(exc)
        item.update(outcome="llm_error", error_code=code if code in runner.RECORD_STOPS else "provider_error")
    except Exception:
        item.update(outcome="llm_error", error_code="provider_error")


def collect_page(target, fetcher, as_of, caps, key, transport, budget, clock):
    item = dict(target, final_url=None, fetched_at=timeutil.iso(clock()), http_status=None, content_sha256=None,
                page_text=None, rules=None, qwen=None, model=schema.QWEN_MODEL, usage=dict.fromkeys(batch.TOKEN_FIELDS, 0))
    if key and key in item["url"]:
        item.update(url=redact(item["url"], key), outcome="blocked", error_code="credential_in_url")
        return item, False
    try:
        page = fetcher.fetch(target["url"])
        if page.provenance != "live_verified":
            raise common.Stop("invalid_policy_envelope")
        doc = page.doc()
    except common.Stop as exc:
        code = str(exc)
        http_status = getattr(exc, "http_status", None)
        if code in BLOCK_CODES or http_status in BLOCK_STATUSES:
            outcome = "blocked"
        elif code in {"denied_robots", "invalid_robots"}:
            outcome = "robots_disallowed"
        else:
            outcome = "fetch_error"
        final_url = getattr(exc, "final_url", None)
        if key and final_url and key in final_url:
            final_url = redact(final_url, key)
        item.update(outcome=outcome, http_status=http_status, final_url=final_url,
                    error_code=code if code in FETCH_CODES else "fetch_error")
        return item, False
    except Exception:
        item["outcome"] = "fetch_error"
        return item, False
    if key and key in page.final_url:
        item.update(outcome="blocked", error_code="credential_in_url", http_status=page.http_status)
        return item, False
    item.update(final_url=page.final_url, http_status=page.http_status)
    try:
        doc, truncated, partial = prepare_doc(doc, caps["max_page_utf8_bytes"], key)
        item.update(content_type=page.content_type, content_sha256=page.body_sha256,
                    source_content_hash=page.source_content_hash, bytes_len=len(page.body), page_text=doc.text(),
                    segment_kinds=[s.kind for s in doc.segments], segment_headings=[s.heading for s in doc.segments],
                    title=doc.title, og_title=doc.meta["og:title"], text_truncated=truncated, partial_segment=partial)
        item["rules"] = runner.baseline(doc, as_of)
    except Exception:
        item.update(outcome="fetch_error", error_code="page_processing_error")
        return item, True
    extract(doc, as_of, caps, key, transport, budget, item)
    return item, True


def run(root, *, config=CONFIG, probe=probe_proxy, fetcher_factory=policy.PolicyFetcher, transport=runner.qwen_provider,
        key_loader=common.load_qwen_key, clock=timeutil.utcnow):
    started = clock()
    folder = root / "handoff/qwen"
    folder.mkdir(parents=True, exist_ok=True)
    record = dict(started_at=timeutil.iso(started), outcome="error", batch_id=None, pages_ok=0, pages_failed=0,
                  llm_calls=0, tokens=dict.fromkeys(batch.TOKEN_FIELDS, 0))
    try:
        with status.locks(root / "locks", ("daily", "collector"), 30 * 60) as available:
            if not available:
                record["outcome"] = "collector_busy"
            elif not probe():
                record["outcome"] = "proxy_unavailable"
            else:
                collect_run(root, folder, config, fetcher_factory, transport, key_loader, clock, started, record)
    except Exception:
        # Exception messages can include provider payloads or URL credentials.
        record["outcome"] = "error"
    finally:
        record["finished_at"] = timeutil.iso(clock())
        status.update(root, "last_bot_run", record)
        with (folder / "usage.jsonl").open("ab") as output:
            output.write(status.json_line(record))
            output.flush()
            os.fsync(output.fileno())
        print("qwen bot " + record["outcome"], flush=True)
    return record


def collect_run(root, folder, config, fetcher_factory, transport, key_loader, clock, started, record):
    caps = status.read_json(config, {})
    if not batch.valid_caps(caps):
        raise ValueError("invalid_caps")
    fetcher = fetcher_factory(require_proxy=True, timeout=caps["timeout_seconds"])
    try:
        key = key_loader()
    except common.Stop:
        key = ""
    checks_path = folder / "state/success.json"
    cutoff = timeutil.iso(started - dt.timedelta(hours=20))
    recent = {url: when for url, when in status.read_json(checks_path, {}).items() if when >= cutoff}
    with tempfile.TemporaryDirectory(prefix="pb-qwen-snapshot-") as tmp:
        snapshot = Path(tmp) / "service.sqlite3"
        batch.copy_database(batch.service_path(root), snapshot)
        with closing(batch.readonly(snapshot)) as conn:
            targets = list(select_targets(conn, started, recent))

    items = []
    for url, target in targets[:caps["max_pages_per_run"]]:
        if record["llm_calls"] >= caps["max_llm_calls_per_run"]:
            break
        as_of = clock().astimezone(timeutil.KST).date().isoformat()
        item, fetched = collect_page(target, fetcher, as_of, caps, key, transport, record, clock)
        if fetched:
            recent[url] = item["fetched_at"]
        for field, value in item["usage"].items():
            record["tokens"][field] += value
        record["pages_ok" if item["outcome"] == "ok" else "pages_failed"] += 1
        items.append(item)
    record["batch_id"] = batch.write_batch(folder / "inbox", items, clock(), runner.runtime_commit(), caps,
                                           record["llm_calls"], record["tokens"])
    status.atomic_json(checks_path, recent)
    record["outcome"] = "ok"


def main(argv=None, *, lock_held=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=db.data_root())
    args = parser.parse_args(argv)
    guard = nullcontext(True) if lock_held else status.locks(args.root / "locks", ("qwen_bot",))
    try:
        with guard as available:
            if not available:
                print("qwen bot already running")
                return 0
            return int(run(args.root)["outcome"] == "error")
    except Exception:
        print("qwen bot status_error")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
