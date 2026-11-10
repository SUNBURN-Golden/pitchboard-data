"""Standalone optional extraction; no live collector or DB handles accepted."""
from __future__ import annotations

import dataclasses
import datetime as dt
import time
import urllib.error
import urllib.request

from .common import CONFIG, Stop, digest, dumps, load_config, load_key, loads, utcnow
from .policy import PolicyFetcher, canonical_url
from .schema import VERSION, PROMPT_HASH, SCHEMA_HASH, request_body, response
from .state import State
from ..net import NoRedirect
from ..parse import PARSER_VERSION
from ...profiles import pitchboard as prof
from ...model import EMAIL_RE, PHONE_RE

ENDPOINT = "https://api.deepseek.com/chat/completions"


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


def provider(body, key, timeout):
    """Fixed endpoint, verified TLS, no proxies, redirects, tools, or retries."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    req = urllib.request.Request(ENDPOINT, data=dumps(body).encode("utf-8"), method="POST",
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


class ShadowRunner:
    def __init__(self, config=CONFIG, clock=utcnow):
        self.config_path, self.clock = config, clock

    def run(self, *, shadow_root, url=None, page=None, as_of=None, fetcher=None,
            transport=None, key_loader=None, extra_protected=(), target=None, digest_languages=()):
        """Disabled path has no side effects. A Page is accepted for cached eval only."""
        try:
            config = load_config(self.config_path, self.clock())
            requested = canonical_url(url if page is None else page.requested_url)
            if page is not None and page.provenance != "offline_cached":
                raise Stop("invalid_policy_envelope")
            if page is None:
                state = State(shadow_root, extra_protected)
                with state.locked() as conn:
                    state.preflight(conn, load_config(self.config_path, self.clock()), self.clock())
                # Only the policy fetcher may assert live verification.
                if fetcher is not None and not isinstance(fetcher, PolicyFetcher):
                    raise Stop("invalid_fetcher")
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
            if key in dumps(body):
                raise Stop("credential_in_page")
            config_hash = digest(dumps(config))
            dedup = digest(dumps([page.final_url, page.body_sha256, as_of, PROMPT_HASH, SCHEMA_HASH,
                                  config_hash, page.provenance]))
            with state.locked() as conn:
                # Honor disabling/config changes even when waiting for another process.
                current = load_config(self.config_path, self.clock())
                if current != config:
                    raise Stop("config_changed")
                now = self.clock()
                attempt, day, cost = state.reserve(conn, config, dedup, now)
                experiment_id = conn.execute("SELECT value FROM meta WHERE key='experiment_id'").fetchone()[0]
                record = {"schema": VERSION, "experiment_id": experiment_id, "attempt_id": attempt,
                          "timestamp_utc": now.isoformat(), "budget_day": day,
                          "requested_url": requested, "final_url": page.final_url, "redirect_chain": list(page.hops),
                          "provenance": page.provenance, "body_sha256": page.body_sha256,
                          "text_sha256": digest(doc.text()), "parser_version": PARSER_VERSION,
                          "prompt_sha256": PROMPT_HASH, "schema_sha256": SCHEMA_HASH,
                          "config_sha256": config_hash, "as_of": as_of,
                          "as_of_source": "frozen cohort KST calendar date" if page.provenance == "offline_cached" else "UTC calendar date",
                          "baseline": rules, "gap_target": target,
                          "requested_model": config["model"], "returned_model": None,
                          "reserved_usd": cost, "charged_ceiling_usd": cost, "usage": None,
                          "fields": None, "validation_reasons": {}, "outcome": "unknown"}
                start = time.monotonic()
                try:
                    dispatch = self.clock()
                    if load_config(self.config_path, dispatch) != config:
                        raise Stop("config_changed")
                    if dispatch.date().isoformat() != day or dispatch < now:
                        raise Stop("dispatch_clock_changed")
                    raw = (transport or provider)(body, key, config["timeout_seconds"])
                    if key in dumps(raw):
                        raise Stop("credential_in_response")
                    fields, reasons, usage = response(raw, doc, as_of, config)
                    record.update(fields=fields, validation_reasons=reasons, usage=usage,
                                  returned_model=raw["model"], outcome="ok")
                except Stop as exc:
                    # Only local fixed reasons; an injected transport cannot leak text.
                    allowed = {"provider_http_error", "provider_too_large", "provider_error", "duplicate_json_key",
                               "invalid_json_number", "model_mismatch", "invalid_usage", "usage_invariant",
                               "invalid_completion", "invalid_model_json", "invalid_schema", "credential_in_response",
                               "dispatch_clock_changed", "inactive_window", "disabled", "invalid_config", "config_changed",
                               "budget_exhausted", "expired_pricing"}
                    record["outcome"] = str(exc) if str(exc) in allowed else "provider_error"
                except Exception:
                    record["outcome"] = "provider_error"
                record["latency_ms"] = round((time.monotonic() - start) * 1000)
                if key in dumps(record):
                    raise Stop("credential_in_record")
                state.finish(conn, record)
                return record
        except Stop as exc:
            return {"outcome": "skipped", "reason": str(exc)}
        except Exception:
            return {"outcome": "skipped", "reason": "shadow_error"}
