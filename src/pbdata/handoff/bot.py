"""Read-only source selection, proxy-only fetching, and LLM batch publication.

The extraction provider is Codex (GPT-6.2-Sol, reasoning effort high) through the same tool-less ``codex exec``
call the shadow job uses (PR #62). The prompt, request messages, grounding checks and the batch/item schema are
unchanged; each item and run records the provider and model. The former Qwen path is kept only as an explicit
``--provider qwen`` option (not a fallback). Folder and file names (handoff/qwen, config/qwen_bot.json,
scripts/qwen_bot.sh, item key "qwen") are kept so the daemon, replay, cron and existing batches keep working.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import dataclasses
import datetime as dt
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from pbdata import db, timeutil
from pbdata.collect import egress
from pbdata.collect.net import junk_page_url, off_topic_page_url
from pbdata.collect.parse import Doc
from pbdata.collect.shadow import codex, common, policy, runner, schema
from pbdata.model import EMAIL_RE, PHONE_RE

from . import batch, status

CONFIG = Path(__file__).resolve().parents[3] / "config/qwen_bot.json"
BLOCK_STATUSES = {401, 402, 403, 429, 451}
BLOCK_CODES = {"access_wall", "blocked_host", "blocked_network", "host_halted"}
FETCH_CODES = BLOCK_CODES | {"denied_robots", "invalid_robots", "denied_http", "unsupported_content", "empty_page",
                             "bad_url", "redirect_limit", "invalid_transport_body", "fetch_error"}
PROVIDER = "codex"
# provider -> (transport, requested model, key loader or None, reasoning effort)
PROVIDERS = {
    "codex": (codex.codex_provider, codex.CODEX_MODEL, None, codex.CODEX_EFFORT),
    # Dead option kept for reference; never used as a fallback.
    "qwen": (runner.qwen_provider, schema.QWEN_MODEL, common.load_qwen_key, None),
}
# Codex at effort high takes ~10-60 s per page, so a run is bounded in time (the bot holds the daily and collector
# locks while it runs) and extracts with a few parallel Codex turns. Unvisited targets are picked up next run,
# most urgent first.
DEFAULT_TIME_BUDGET_SECONDS = 3 * 60 * 60
DEFAULT_WORKERS = 4
DEFAULT_LOCK_WAIT_SECONDS = 30 * 60
_BUDGET_LOCK = threading.Lock()


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
        print(f"handoff bot proxy probe {attempt + 1}/5 failed", flush=True)
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
            live.urgent, live.source_id IS NOT NULL AS is_live, s.last_success_at,
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
        # Same rule as the collector (#63): institutional/off-topic sections (kent.edu hr, admissions, calendars, ...)
        # are not fetched unless they back a live call; they made the 18:00 run take hours and hold the collector lock.
        if not row["is_live"] and off_topic_page_url(url):
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
    """Retain complete segments when possible, with the same text used for the LLM and replay."""
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


def reserve_call(budget, max_calls):
    """Count one LLM call against the run, atomically across extraction workers."""
    with _BUDGET_LOCK:
        if batch.exceeds(budget["llm_calls"] + 1, max_calls):
            return False
        budget["llm_calls"] += 1
        return True


def extract(doc, as_of, caps, key, transport, budget, item, clock=timeutil.utcnow, pricing=None, provider=PROVIDER):
    if not key and PROVIDERS[provider][2] is not None:
        item.update(outcome="llm_error", error_code="key_unavailable")
        return
    max_calls = caps.get("max_llm_calls_per_run")
    if batch.exceeds(budget["llm_calls"] + 1, max_calls):
        item.update(outcome="skipped_cap", error_code="llm_cap")
        return
    # Codex gets the same messages as every other provider; only Qwen needs its own model/thinking fields.
    leaked = (lambda value: key in common.dumps(value)) if key else (lambda value: False)
    try:
        body = schema.request_body(doc, as_of, caps, provider="qwen" if provider == "qwen" else "deepseek")
        for repair in (False, True):
            if leaked(body):
                raise common.Stop("credential_in_response" if repair else "credential_in_request")
            if not reserve_call(budget, max_calls):
                item.update(outcome="llm_error" if repair else "skipped_cap",
                            error_code="repair_skipped_cap" if repair else "llm_cap")
                return
            item["repair_attempted"] = repair
            rates = common.pricing_at(pricing, clock())
            call = common.call_usage(provider, None, rates)
            item["calls"].append(call)
            started = time.monotonic()
            raw = transport(body, key, caps["timeout_seconds"])
            call["seconds"] = round(time.monotonic() - started, 1)
            call.update(common.call_usage(provider, raw, rates))
            for field, value in (call["usage"] or {}).items():
                item["usage"][field] += value
            if leaked(raw):
                raise common.Stop("credential_in_response")
            try:
                # Both passes use the shadow shape tolerances, including missing keys as null.
                fields, reasons, usage = schema.response(raw, doc, as_of, caps, provider=provider,
                                                         capture_repair_candidate=True)
            except schema.SchemaRepairCandidate as exc:
                if repair or str(exc) not in runner.REPAIR_SCHEMA_CODES:
                    raise
                if leaked(exc.prior):
                    raise common.Stop("credential_in_response")
                if batch.exceeds(budget["llm_calls"] + 1, max_calls):
                    item.update(outcome="llm_error", error_code="repair_skipped_cap")
                    return
                body = schema.repair_body(body, exc.prior, str(exc))
                continue
            if leaked(fields):
                raise common.Stop("credential_in_response")
            if "reasoning_tokens" in usage:
                call["reasoning_tokens"] = usage["reasoning_tokens"]
            # The "qwen" key is the batch schema's advisory-value field; "provider"/"model" say who produced it.
            item.update(qwen=fields, validation_reasons=reasons, returned_model=raw["model"], outcome="ok",
                        repair_ok=repair)
            return
    except common.Stop as exc:
        code = str(exc)
        item.update(outcome="llm_error", error_code=code if code in runner.RECORD_STOPS else "provider_error")
    except Exception:
        item.update(outcome="llm_error", error_code="provider_error")


def collect_page(target, fetcher, as_of, caps, key, transport, budget, clock, pricing=None, provider=PROVIDER):
    item, fetched, doc = fetch_page(target, fetcher, as_of, caps, key, clock, provider)
    if doc is not None:
        extract(doc, as_of, caps, key, transport, budget, item, clock, pricing, provider)
    return item, fetched


def fetch_page(target, fetcher, as_of, caps, key, clock, provider=PROVIDER):
    """Fetch and prepare one page. Returns (item, fetched, doc); doc is None when there is nothing to extract."""
    _, model, _, effort = PROVIDERS[provider]
    item = dict(target, final_url=None, fetched_at=timeutil.iso(clock()), http_status=None, content_sha256=None,
                page_text=None, rules=None, qwen=None, provider=provider, model=model,
                usage=dict.fromkeys(batch.TOKEN_FIELDS, 0), calls=[], repair_attempted=False, repair_ok=False)
    if effort:
        item["reasoning_effort"] = effort
    if key and key in item["url"]:
        item.update(url=redact(item["url"], key), outcome="blocked", error_code="credential_in_url")
        return item, False, None
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
        return item, False, None
    except Exception:
        item["outcome"] = "fetch_error"
        return item, False, None
    if key and key in page.final_url:
        item.update(outcome="blocked", error_code="credential_in_url", http_status=page.http_status)
        return item, False, None
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
        return item, True, None
    return item, True, doc


def run(root, *, config=CONFIG, probe=probe_proxy, fetcher_factory=policy.PolicyFetcher, transport=None,
        key_loader=None, clock=timeutil.utcnow, provider=PROVIDER, time_budget=DEFAULT_TIME_BUDGET_SECONDS,
        workers=DEFAULT_WORKERS, max_pages=None, lock_wait=DEFAULT_LOCK_WAIT_SECONDS, monotonic=time.monotonic):
    default_transport, model, default_key_loader, effort = PROVIDERS[provider]
    transport = transport or default_transport
    key_loader = key_loader or default_key_loader or (lambda: "")
    started = clock()
    folder = root / "handoff/qwen"
    folder.mkdir(parents=True, exist_ok=True)
    record = dict(started_at=timeutil.iso(started), outcome="error", batch_id=None, pages_ok=0, pages_failed=0,
                  llm_calls=0, tokens=dict.fromkeys(batch.TOKEN_FIELDS, 0), estimated_cost_usd="0",
                  provider=provider, model=model, reasoning_effort=effort)
    run_opts = dict(time_budget=time_budget, workers=max(1, int(workers)), max_pages=max_pages, monotonic=monotonic,
                    provider=provider)
    try:
        with status.locks(root / "locks", ("daily", "collector"), lock_wait) as available:
            if not available:
                record["outcome"] = "collector_busy"
            elif not probe():
                record["outcome"] = "proxy_unavailable"
            else:
                collect_run(root, folder, config, fetcher_factory, transport, key_loader, clock, started, record,
                            **run_opts)
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
        print(f"handoff bot ({provider}) {record['outcome']} pages_ok={record['pages_ok']} "
              f"pages_failed={record['pages_failed']} llm_calls={record['llm_calls']}"
              + (" stopped=time_budget" if record.get("stopped") else ""), flush=True)
    return record


def collect_run(root, folder, config, fetcher_factory, transport, key_loader, clock, started, record, *,
                time_budget=DEFAULT_TIME_BUDGET_SECONDS, workers=1, max_pages=None, monotonic=time.monotonic,
                provider=PROVIDER):
    caps = status.read_json(config, {})
    pricing = common.pricing_snapshot(caps.pop("pricing", None)) if isinstance(caps, dict) else None
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
    with tempfile.TemporaryDirectory(prefix="pb-bot-snapshot-") as tmp:
        snapshot = Path(tmp) / "service.sqlite3"
        batch.copy_database(batch.service_path(root), snapshot)
        with closing(batch.readonly(snapshot)) as conn:
            targets = list(select_targets(conn, started, recent))

    limit = caps.get("max_pages_per_run")
    if max_pages is not None:
        limit = max_pages if limit is None else min(limit, max_pages)
    deadline = None if time_budget is None else monotonic() + time_budget
    items = []
    # Pages are fetched in order on this thread (the fetcher keeps per-host politeness); extraction runs on a small
    # pool. The semaphore keeps fetching at most a few pages ahead of extraction.
    slots = threading.BoundedSemaphore(workers + 1)

    def extract_job(doc, as_of, item):
        try:
            extract(doc, as_of, caps, key, transport, record, item, clock, pricing, provider)
        finally:
            slots.release()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pb-bot-llm") as pool:
        for url, target in targets[:limit]:
            if batch.exceeds(record["llm_calls"] + 1, caps.get("max_llm_calls_per_run")):
                break
            if deadline is not None and monotonic() >= deadline:
                record["stopped"] = "time_budget"
                break
            slots.acquire()
            as_of = clock().astimezone(timeutil.KST).date().isoformat()
            item, fetched, doc = fetch_page(target, fetcher, as_of, caps, key, clock, provider)
            if fetched:
                recent[url] = item["fetched_at"]
            items.append(item)
            if doc is None:
                slots.release()
            else:
                pool.submit(extract_job, doc, as_of, item)
    for item in items:
        for field, value in item["usage"].items():
            record["tokens"][field] += value
        record["estimated_cost_usd"] = common.total_cost(
            [record["estimated_cost_usd"], *(call["estimated_cost_usd"] for call in item["calls"])])
        record["pages_ok" if item["outcome"] == "ok" else "pages_failed"] += 1
    record["targets_total"] = len(targets)
    record["batch_id"] = batch.write_batch(folder / "inbox", items, clock(), runner.runtime_commit(), caps,
                                           record["llm_calls"], record["tokens"], record["estimated_cost_usd"])
    status.atomic_json(checks_path, recent)
    record["outcome"] = "ok"


def main(argv=None, *, lock_held=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=db.data_root())
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default=PROVIDER)
    parser.add_argument("--time-budget", type=int, default=DEFAULT_TIME_BUDGET_SECONDS,
                        help="stop fetching new pages after this many seconds (in-flight extractions finish)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="parallel LLM extractions")
    parser.add_argument("--max-pages", type=int, default=None, help="process at most this many targets")
    parser.add_argument("--lock-wait", type=int, default=DEFAULT_LOCK_WAIT_SECONDS,
                        help="seconds to wait for the daily and collector locks")
    args = parser.parse_args(argv)
    guard = nullcontext(True) if lock_held else status.locks(args.root / "locks", ("qwen_bot",))
    try:
        with guard as available:
            if not available:
                print("handoff bot already running")
                return 0
            return int(run(args.root, provider=args.provider, time_budget=args.time_budget, workers=args.workers,
                           max_pages=args.max_pages, lock_wait=args.lock_wait)["outcome"] == "error")
    except Exception:
        print("handoff bot status_error")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
