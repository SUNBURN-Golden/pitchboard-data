"""Standalone optional extraction; no live collector or DB handles accepted."""
from __future__ import annotations

import dataclasses
import datetime as dt
import time
import subprocess
from pathlib import Path
import urllib.error
import urllib.request

from urllib.parse import urlsplit

from .common import CONFIG, Stop, digest, dumps, load_config, load_key, load_qwen_key, loads, utcnow
from .policy import PolicyFetcher, canonical_url
from .schema import VERSION, PROMPT_HASH, SCHEMA_HASH, SchemaRepairCandidate, repair_body, request_body, response
from .state import State
from . import source_cache
from ..net import NoRedirect
from ..parse import PARSER_VERSION
from ...profiles import pitchboard as prof
from ...model import EMAIL_RE, PHONE_RE

ENDPOINT = "https://api.deepseek.com/chat/completions"
QWEN_ENDPOINT = "https://token-plan.maas.qwencloudapi.com/compatible-mode/v1/chat/completions"
ALLOWED_PROVIDER_HOSTS = frozenset({"api.deepseek.com", "token-plan.maas.qwencloudapi.com"})
FALLBACK_CODES = frozenset({"provider_error", "provider_http_error"})
REPAIR_SCHEMA_CODES = frozenset({"invalid_schema", "invalid_model_json", "duplicate_json_key", "invalid_json_number"})
RECORD_STOPS = frozenset({
    "provider_http_error", "provider_too_large", "provider_error", "duplicate_json_key",
    "invalid_json_number", "model_mismatch", "invalid_usage", "usage_invariant",
    "invalid_completion", "invalid_model_json", "invalid_schema", "credential_in_response",
    "dispatch_clock_changed", "inactive_window", "disabled", "invalid_config", "config_changed",
    "budget_exhausted", "expired_pricing"})


def baseline(doc, as_of):
    """Pure rules plus the existing replay's promote_calls projection."""
    exts = prof.extract(doc)
    labs = prof.label_doc(doc, exts, dt.date.fromisoformat(as_of), source_official=True)
    skip = {"stale_prior_year", "results_not_call", "past_deadline", "not_writing_call"}
    rec = next((l for l in labs if l.label == "recruitment"), None)
    rules = any(l.label == "rules" for l in labs)
    allowed = (rec is not None or rules) and not (rec and rec.status in skip)
    closes = sorted({e.value["date"] for e in exts if e.field == "deadline.closes_on"})
    keys = [date for date in closes if date >= as_of] if allowed else []
    if allowed and not keys and any(e.field == "status.declared" and e.value.get("declared") == "standing" for e in exts):
        keys = ["standing"]
    raw_exts = [dataclasses.asdict(e) for e in exts]
    # Rules values can contain source text/contact data: retain only sanitized JSON.
    raw_exts = loads(PHONE_RE.sub("[phone]", EMAIL_RE.sub("[email]", dumps(raw_exts))))
    return {"extractor_version": prof.EXTRACTOR_VERSION, "labeler_version": prof.LABELER_VERSION,
            "source_official": True, "extensions": raw_exts,
            "labels": [dataclasses.asdict(l) for l in labs], "promote_calls": keys,
            "real_call": any(l.label in ("recruitment", "rules") and l.status != "not_writing_call" for l in labs),
            "current_open": bool(keys), "deadline": closes}


def runtime_commit():
    try:
        value = subprocess.check_output(["git", "-C", str(Path(__file__).resolve().parents[4]), "rev-parse", "HEAD"],
                                        text=True, stderr=subprocess.DEVNULL).strip()
        return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else "unknown"
    except Exception:
        return "unknown"


def provider(body, key, timeout, endpoint=ENDPOINT):
    """Fixed allowlisted HTTPS endpoint, verified TLS, no proxies, redirects, tools, or retries."""
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or parts.port not in (None, 443) or parts.hostname not in ALLOWED_PROVIDER_HOSTS:
        raise Stop("blocked_network")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    req = urllib.request.Request(endpoint, data=dumps(body).encode("utf-8"), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            if resp.status != 200 or resp.headers.get_content_type() != "application/json":
                raise Stop("provider_http_error")
            data = resp.read(262145)
            if len(data) > 262144:
                raise Stop("provider_too_large")
        return loads(data)
    except Stop:
        raise
    except Exception:
        # No exception text/headers/body: provider errors can contain credentials.
        raise Stop("provider_error") from None


def qwen_provider(body, key, timeout):
    return provider(body, key, timeout, endpoint=QWEN_ENDPOINT)


def _stop_code(exc):
    return str(exc) if str(exc) in RECORD_STOPS else "provider_error"


class ShadowRunner:
    def __init__(self, config=CONFIG, clock=utcnow):
        self.config_path, self.clock = config, clock

    def _dispatch_time(self, config, day, reserved_at):
        dispatch = self.clock()
        if load_config(self.config_path, dispatch) != config:
            raise Stop("config_changed")
        if dispatch.date().isoformat() != day or dispatch < reserved_at:
            raise Stop("dispatch_clock_changed")
        return dispatch

    def run(self, *, shadow_root, url=None, page=None, as_of=None, fetcher=None,
            transport=None, key_loader=None, fallback_transport=None, fallback_key_loader=None,
            extra_protected=(), target=None, digest_languages=(), batch_day=None):
        """Disabled path has no side effects. A Page is accepted for cached eval only."""
        try:
            config = load_config(self.config_path, self.clock())
            if not isinstance(digest_languages, (tuple, list)) or set(digest_languages) - {"ko", "en"}:
                raise Stop("invalid_digest_languages")
            if batch_day is not None and self.clock().date().isoformat() != batch_day:
                raise Stop("dispatch_clock_changed")
            requested = canonical_url(url if page is None else page.requested_url)
            if page is not None and page.provenance != "offline_cached":
                raise Stop("invalid_policy_envelope")
            if page is None:
                state = State(shadow_root, extra_protected)
                with state.locked() as conn:
                    state.preflight(conn, load_config(self.config_path, self.clock()), self.clock())
                known_hash = target.get("content_hash") if target else None
                if target:
                    # Reapply export eligibility on reuse; a caller cannot turn a
                    # latest denied observation into a cached successful source.
                    from .planner import plan, EXPORT_SCHEMA
                    checked = plan({"schema": EXPORT_SCHEMA, "exported_at_utc": self.clock().isoformat(),
                                    "rows": [{k: v for k, v in t.items() if not k.startswith("shadow_")} for t in target["targets"]]}, limit=1)
                    if (not checked["targets"] or checked["targets"][0]["url"] != requested
                            or checked["targets"][0]["content_hash"] != known_hash):
                        raise Stop("ineligible_source")
                # ``known_hash`` is the service ``pb_raw_ref.content_hash``; the
                # cache keys on that same service contract, never raw bytes.
                page = source_cache.load(state.root, requested, known_hash, self.clock(), config["max_page_utf8_bytes"])
                # Only the policy fetcher may assert live verification.
                if fetcher is not None and not isinstance(fetcher, PolicyFetcher):
                    raise Stop("invalid_fetcher")
                if page is None:
                    page = (fetcher or PolicyFetcher(egress=config["egress"])).fetch(requested)
            doc = page.doc()
            if len(doc.text().encode("utf-8")) > config["max_page_utf8_bytes"]:
                raise Stop("page_too_large")
            as_of = as_of or self.clock().date().isoformat()
            if dt.date.fromisoformat(as_of).isoformat() != as_of:
                raise Stop("invalid_as_of")
            rules = baseline(doc, as_of)
            body = request_body(doc, as_of, config)
            # Validate root before reading credentials; do not inherit PB_DATA_ROOT.
            state = State(shadow_root, extra_protected)
            key = (key_loader or load_key)()
            # The key is never sent as model data, nor allowed into persisted values.
            if key in dumps(body) or key in dumps(target) or key in requested:
                raise Stop("credential_in_page")
            config_hash = digest(dumps(config))
            dedup = digest(dumps([page.final_url, page.body_sha256, as_of, PROMPT_HASH, SCHEMA_HASH,
                                  config_hash, "offline_cached" if page.provenance == "offline_cached" else "policy_source"]))
            with state.locked() as conn:
                # Honor disabling/config changes even when waiting for another process.
                current = load_config(self.config_path, self.clock())
                if current != config:
                    raise Stop("config_changed")
                now = self.clock()
                if batch_day is not None and now.date().isoformat() != batch_day:
                    raise Stop("dispatch_clock_changed")
                cache_id = source_cache.save(state, page, doc, now, key)
                attempt, day, cost = state.reserve(conn, config, dedup, now)
                experiment_id = conn.execute("SELECT value FROM meta WHERE key='experiment_id'").fetchone()[0]
                record = {"schema": VERSION, "experiment_id": experiment_id, "attempt_id": attempt,
                          "timestamp_utc": now.isoformat(), "budget_day": day,
                          "requested_url": requested, "final_url": page.final_url, "redirect_chain": list(page.hops),
                          "provenance": page.provenance, "body_sha256": page.body_sha256,
                          "source_content_hash": page.source_content_hash,
                          "text_sha256": digest(doc.text()), "parser_version": PARSER_VERSION,
                          "source_cache_key": cache_id,
                          "source_observed_at_utc": getattr(page, "observed_at_utc", "") or now.isoformat(),
                          "source_changed": bool(target and target.get("content_hash") and target["content_hash"] != page.source_content_hash),
                          "prompt_sha256": PROMPT_HASH, "schema_sha256": SCHEMA_HASH,
                          "config_sha256": config_hash, "config_snapshot": config,
                          "runtime_commit": runtime_commit(), "as_of": as_of,
                          "as_of_source": "frozen cohort KST calendar date" if page.provenance == "offline_cached" else "UTC calendar date",
                          "baseline": rules, "gap_target": target,
                          "requested_model": config["model"], "returned_model": None,
                          "provider": "deepseek", "provider_calls": 1, "fallback_from": None,
                          "repair_attempted": False, "repair_outcome": None,
                          "reserved_usd": cost, "charged_ceiling_usd": cost, "usage": None,
                          "fields": None, "validation_reasons": {}, "schema_shape": {}, "outcome": "unknown"}
                start = time.monotonic()
                primary = transport or provider
                # Custom transports stay primary-only unless a fallback is injected.
                fb_transport = fallback_transport or (qwen_provider if transport is None else None)
                secrets = [key]
                repair = None
                try:
                    self._dispatch_time(config, day, now)
                    raw = primary(body, key, config["timeout_seconds"])
                    if key in dumps(raw):
                        raise Stop("credential_in_response")
                    fields, reasons, usage = response(raw, doc, as_of, config,
                                                     schema_shape=record["schema_shape"])
                    record.update(fields=fields, validation_reasons=reasons, usage=usage,
                                  returned_model=raw["model"], outcome="ok", provider="deepseek")
                except Stop as exc:
                    # Only local fixed reasons; an injected transport cannot leak text.
                    record["outcome"] = _stop_code(exc)
                    if isinstance(exc, SchemaRepairCandidate):
                        repair = (exc.prior, primary, key, body)
                except Exception:
                    record["outcome"] = "provider_error"
                if record["outcome"] in FALLBACK_CODES and fb_transport is not None:
                    fb_key = ""
                    try:
                        fb_key = (fallback_key_loader or load_qwen_key)()
                    except Stop:
                        fb_key = ""
                    qwen_body = request_body(doc, as_of, config, provider="qwen")
                    if fb_key and fb_key not in dumps(qwen_body) and fb_key not in dumps(target) and fb_key not in requested:
                        secrets.append(fb_key)
                        try:
                            dispatch = self._dispatch_time(config, day, now)
                            new_cost = state.reserve_extra_call(conn, config, dispatch, attempt)
                            record["reserved_usd"] = new_cost
                            record["charged_ceiling_usd"] = new_cost
                            record["provider_calls"] = 2
                            record["fallback_from"] = record["outcome"]
                            record["provider"] = "qwen"
                            raw = fb_transport(qwen_body, fb_key, config["timeout_seconds"])
                            if any(s in dumps(raw) for s in secrets):
                                raise Stop("credential_in_response")
                            fields, reasons, usage = response(raw, doc, as_of, config, provider="qwen",
                                                             schema_shape=record["schema_shape"])
                            record.update(fields=fields, validation_reasons=reasons, usage=usage,
                                          returned_model=raw["model"], outcome="ok")
                        except Stop as exc:
                            if record["provider_calls"] == 1:
                                # Fallback was not charged; keep the primary failure.
                                record["provider"] = "deepseek"
                                record["fallback_from"] = None
                            else:
                                record["outcome"] = _stop_code(exc)
                                record["provider"] = "qwen"
                                if isinstance(exc, SchemaRepairCandidate):
                                    repair = (exc.prior, fb_transport, fb_key, qwen_body)
                        except Exception:
                            record["outcome"] = "provider_error"
                            record["provider"] = "qwen"
                # Repair runs strictly after the fallback block, so a provider_error
                # outcome here can never re-trigger the fallback.
                if repair is not None:
                    prior, repair_transport, repair_key, original_body = repair
                    try:
                        # The provider envelope may hide credentials behind JSON escapes.
                        prior_json = dumps(prior)
                        if any(s in prior_json for s in secrets):
                            raise Stop("credential_in_response")
                        correction = repair_body(original_body, prior)
                        if any(s in dumps(correction) for s in secrets):
                            raise Stop("credential_in_response")
                        dispatch = self._dispatch_time(config, day, now)
                    except Stop as exc:
                        if str(exc) == "credential_in_response":
                            record["outcome"] = "credential_in_response"
                    except (ValueError, RecursionError):
                        # Fail closed if the parsed JSON cannot be serialized for repair.
                        pass
                    else:
                        try:
                            new_cost = state.reserve_extra_call(conn, config, dispatch, attempt)
                        except Stop as exc:
                            # Only the cap maps to skipped_cap; other guard stops
                            # (halt/clock/config/state) keep invalid_schema with
                            # repair_attempted=false, like the fallback's uncharged guard.
                            if str(exc) == "budget_exhausted":
                                record["repair_outcome"] = "skipped_cap"
                        else:
                            record.update(reserved_usd=new_cost, charged_ceiling_usd=new_cost,
                                          provider_calls=record["provider_calls"] + 1,
                                          repair_attempted=True, repair_outcome="provider_error")
                if record["repair_attempted"]:
                    try:
                        raw = repair_transport(correction, repair_key, config["timeout_seconds"])
                        if any(s in dumps(raw) for s in secrets):
                            raise Stop("credential_in_response")
                        try:
                            fields, reasons, usage = response(raw, doc, as_of, config,
                                                              provider=record["provider"], strict_repair=True)
                        except Stop as exc:
                            if str(exc) in REPAIR_SCHEMA_CODES:
                                record["repair_outcome"] = "invalid_schema"
                            raise
                        record.update(fields=fields, validation_reasons=reasons, usage=usage,
                                      returned_model=raw["model"], outcome="ok", repair_outcome="ok")
                    except Stop as exc:
                        record["outcome"] = _stop_code(exc)
                    except Exception:
                        record["outcome"] = "provider_error"
                from .review import reviews
                record["reviews"] = reviews(record)
                from .digest import drafts
                record["digest_drafts"] = drafts(record, digest_languages)
                record["latency_ms"] = round((time.monotonic() - start) * 1000)
                if any(s in dumps(record) for s in secrets):
                    raise Stop("credential_in_record")
                state.finish(conn, record)
                return record
        except Stop as exc:
            return {"outcome": "skipped", "reason": str(exc)}
        except Exception:
            return {"outcome": "skipped", "reason": "shadow_error"}
