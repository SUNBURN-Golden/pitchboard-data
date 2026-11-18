"""Shadow safety regressions: temporary state and fake transports only."""
import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from pbdata.collect.shadow import common, policy, runner, schema, state
from pbdata.collect.shadow.common import Stop, UTC, digest, dumps
from pbdata.collect.parse import Doc, Segment, parse_html

NOW = dt.datetime(2026, 11, 5, 12, tzinfo=UTC)
URL = "https://example.org/call"
KEY = "sk-SENTINEL-NEVER-LOG-123"
HTML = ("<html><title>Writing submissions 2026</title><h1>Writing submissions</h1>"
        "<p>Submissions are open until December 1, 2026.</p>"
        "<p>We pay USD 100 per story.</p><p>All writers worldwide may apply.</p></html>").encode()
QUOTE = "Submissions are open until December 1, 2026."


def page(url=URL, body=HTML):
    return policy.Page(url, url, body, (url,), "offline_cached")


def proposal():
    return {
        "submission_fee": None,
        "open_call": {"value": {"is_real_call": True, "status": "open"}, "quote": QUOTE},
        "deadline": {"value": {"text": "December 1, 2026", "date": "2026-12-01", "timezone": None}, "quote": QUOTE},
        "pay": {"value": {"text": "USD 100 per story", "state": "paid", "amount_min": "100", "amount_max": "100",
                          "currency": "USD", "unit": "per_story"}, "quote": "We pay USD 100 per story."},
        "eligibility": {"value": {"text": "All writers worldwide", "constraints": [{"type": "region", "text": "worldwide"}]},
                        "quote": "All writers worldwide may apply."}}


def completion(fields=None):
    return {"model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {
        "content": dumps(proposal() if fields is None else fields)}}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 100, "total_tokens": 223}}


def shape_summary(*, missing=(), extra=(), top_type="dict", redacted=0, omitted=0):
    return {"top_type": top_type, "missing": list(missing), "extra": list(extra),
            "extra_redacted_count": redacted, "extra_omitted_count": omitted}


def shape_cases():
    missing = proposal()
    del missing["pay"]
    nonverbatim = proposal()
    nonverbatim["pay"]["value"]["text"] = "value_sentinel"
    cases = [
        (proposal(), "ok", {}, {}),
        (dict.fromkeys(schema.FIELDS), "ok", {}, {}),
        (nonverbatim, "ok", {}, {"pay": "value_not_verbatim"}),
        (missing, "ok", shape_summary(missing=["pay"]), {"pay": "missing_key"}),
        ({}, "ok", shape_summary(missing=schema.FIELDS), dict.fromkeys(schema.FIELDS, "missing_key")),
        ({**proposal(), "extra": "value_sentinel"}, "invalid_schema", shape_summary(extra=["extra"]), {}),
        ({"result": proposal()}, "invalid_schema", shape_summary(missing=schema.FIELDS, extra=["result"]), {}),
    ]
    cases += [([raw], outcome, {**shape, "unwrapped_list": True}, reasons)
              for raw, outcome, shape, reasons in cases]
    cases += [(raw, "invalid_schema", {"top_type": "list", "list_len": len(raw)}, {})
              for raw in ([], [proposal(), proposal()], [proposal(), "value_sentinel"],
                          ["value_sentinel"], [42], [1.5], [True], [False], [None], [[proposal()]])]
    cases.append(("value_sentinel", "invalid_schema", shape_summary(top_type="str"), {}))
    return cases


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.config_path = self.base / "config.json"
        self.config = json.loads(common.CONFIG.read_text())
        self.config.update(enabled=True, window_start_utc="2026-11-05T00:00:00Z", window_end_utc="2026-11-12T00:00:00Z")
        self.write_config()
        self.root = self.base / "shadow"
        self.calls = []
        self.clock = NOW

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self):
        self.config_path.write_text(dumps(self.config))

    def transport(self, body, key, timeout):
        self.assertEqual(key, KEY)
        self.assertNotIn(KEY, dumps(body))
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("tools", body)
        self.calls.append(body)
        return completion()

    def run_page(self, url=URL, **kwargs):
        opts = {"shadow_root": self.root, "url": url, "page": page(url), "transport": self.transport, "key_loader": lambda: KEY}
        opts.update(kwargs)
        return runner.ShadowRunner(self.config_path, clock=lambda: self.clock).run(**opts)

    def artifacts(self):
        return b"".join(p.read_bytes() for p in self.root.rglob("*") if p.is_file())

    def assert_persisted_record(self, result):
        path = next(self.root.glob("data/shadow/llm_extract_*.jsonl"))
        self.assertEqual([json.loads(line) for line in path.read_text().splitlines()], [result])
        with state.State(self.root).locked() as conn:
            rows = [json.loads(r[0]) for r in conn.execute("SELECT result FROM attempts")]
        self.assertEqual(rows, [result])


class ConfigTests(Harness):
    def test_checked_in_flag_is_false_and_no_side_effects(self):
        shipped = json.loads(common.CONFIG.read_text())
        self.assertIs(shipped["enabled"], False)
        self.assertEqual(shipped["monetary_cap"], "enforced")
        self.assertEqual(shipped["egress"], "direct")
        self.assertEqual(shipped["daily_cost_cap_usd"], "5.00")
        self.assertEqual(shipped["daily_call_cap"], 50)
        with patch.object(runner, "load_key", side_effect=AssertionError("credentials touched")), patch.object(policy.PolicyFetcher, "fetch", side_effect=AssertionError("network")):
            result = runner.ShadowRunner().run(shadow_root=self.root, url=URL)
        self.assertEqual(result["reason"], "disabled")
        self.assertFalse(self.root.exists())

    def test_missing_invalid_false_config_all_skip_without_secrets_or_network(self):
        cases = [None, {}, {"enabled": False}, {"enabled": "true"}, {**self.config, "extra": 1},
                 {**self.config, "daily_call_cap": True}, {**self.config, "daily_call_cap": 0},
                 {**self.config, "daily_cost_cap_usd": "0"}, {**self.config, "daily_cost_cap_usd": "NaN"},
                 {**self.config, "daily_cost_cap_usd": "-1"}, {**self.config, "daily_cost_cap_usd": None},
                 {**self.config, "monetary_cap": "none"}, {**self.config, "monetary_cap": "unlimited"},
                 {**self.config, "monetary_cap": "none", "daily_cost_cap_usd": "0"},
                 {**self.config, "monetary_cap": "none", "daily_cost_cap_usd": "none"},
                 {k: v for k, v in self.config.items() if k != "monetary_cap"},
                 {**self.config, "egress": "open"}, {**self.config, "egress": "198.18.0.0/15"},
                 {k: v for k, v in self.config.items() if k != "egress"},
                 {**self.config, "mode": "live"},
                 {**self.config, "window_end_utc": "2026-11-14T00:00:00Z"},
                 {**self.config, "window_end_utc": "2026-11-06"},
                 {**self.config, "window_start_utc": "2026-11-06T00:00:00Z"},
                 {**self.config, "window_end_utc": "2026-11-05T01:00:00Z"},
                 {**self.config, "pricing": {**self.config["pricing"], "valid_until_utc": "2026-11-11T00:00:00Z"}}]
        for c in cases:
            with self.subTest(config=c):
                if c is None:
                    self.config_path.unlink(missing_ok=True)
                else:
                    self.config_path.write_text(dumps(c))
                result = self.run_page(key_loader=lambda: self.fail("credential lookup"))
                self.assertEqual(result["outcome"], "skipped")
                self.assertFalse(self.root.exists())
        self.assertFalse(self.calls)

    def test_invalid_duplicate_config_and_stale_rates(self):
        self.config_path.write_text('{"enabled":true,"enabled":true}')
        self.assertEqual(self.run_page()["reason"], "duplicate_json_key")
        self.config["pricing"]["input_usd_per_million_ceiling"] = "0.01"
        self.write_config()
        self.assertEqual(self.run_page()["reason"], "invalid_config")


class BudgetTests(Harness):
    def test_call_cap_restart_failed_attempts_and_utc_rollover(self):
        self.config.update(daily_call_cap=2, daily_cost_cap_usd="100")
        self.write_config()
        def fail(*_):
            self.calls.append(1)
            raise TimeoutError(KEY)
        self.assertEqual(self.run_page(URL + "1", transport=fail)["outcome"], "provider_error")
        self.assertEqual(self.run_page(URL + "2")["outcome"], "ok")
        self.assertEqual(self.run_page(URL + "3")["reason"], "budget_exhausted")
        self.clock += dt.timedelta(days=1)
        self.assertEqual(self.run_page(URL + "4")["outcome"], "ok")
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn(KEY.encode(), self.artifacts())

    def test_cost_cap_exact_boundary_and_conservative_default(self):
        unit = state.reservation(self.config)
        self.assertEqual(unit, Decimal("0.3024576"))
        self.config["daily_cost_cap_usd"] = str(unit * 2)
        self.write_config()
        for i in range(2):
            self.assertEqual(self.run_page(URL + str(i))["outcome"], "ok")
        self.assertEqual(self.run_page(URL + "last")["reason"], "budget_exhausted")
        self.assertEqual(len(self.calls), 2)

    def test_insufficient_cost_zero_provider_calls(self):
        self.config["daily_cost_cap_usd"] = "0.3024575"
        self.write_config()
        self.assertEqual(self.run_page()["reason"], "budget_exhausted")
        self.assertFalse(self.calls)

    def test_default_five_dollars_allows_sixteen_calls(self):
        for i in range(16):
            self.assertEqual(self.run_page(URL + str(i))["outcome"], "ok")
        self.assertEqual(self.run_page(URL + "17")["reason"], "budget_exhausted")
        self.assertEqual(len(self.calls), 16)

    def test_monetary_cap_none_keeps_call_cap_ceiling_and_pricing(self):
        self.config.update(monetary_cap="none", daily_cost_cap_usd=None, daily_call_cap=17, egress="sandbox")
        self.write_config()
        for i in range(17):
            result = self.run_page(URL + str(i))
            self.assertEqual(result["outcome"], "ok")
            self.assertEqual(result["charged_ceiling_usd"], "0.3024576")
        self.assertEqual(self.run_page(URL + "stop")["reason"], "budget_exhausted")
        self.assertEqual(len(self.calls), 17)
        self.config["pricing"]["valid_until_utc"] = "2026-11-11T00:00:00Z"
        self.write_config()
        self.root = self.base / "expired-none"
        self.assertEqual(self.run_page(URL + "later")["reason"], "expired_pricing")
        self.assertEqual(len(self.calls), 17)

    def test_monetary_cap_none_concurrency_stops_at_call_cap(self):
        self.config.update(monetary_cap="none", daily_cost_cap_usd=None, daily_call_cap=2)
        self.write_config()
        with state.State(self.root).locked():
            pass
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda i: self.run_page(URL + str(i)), range(8)))
        self.assertEqual(sum(r["outcome"] == "ok" for r in results), 2)
        self.assertEqual(len(self.calls), 2)

    def test_concurrent_call_and_cost_caps(self):
        for binding in ("call", "cost"):
            with self.subTest(binding=binding):
                self.root = self.base / binding
                self.calls.clear()
                self.config.update(daily_call_cap=3 if binding == "call" else 50,
                                   daily_cost_cap_usd="100" if binding == "call" else str(state.reservation(self.config) * 3))
                self.write_config()
                # Initialize once, then independent State connections and locks compete.
                with state.State(self.root).locked():
                    pass
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(lambda i: self.run_page(URL + str(i)), range(12)))
                self.assertEqual(sum(r["outcome"] == "ok" for r in results), 3)
                self.assertEqual(len(self.calls), 3)

    def test_crashed_reservation_counts_and_is_deduplicated(self):
        self.config["daily_call_cap"] = 1
        self.write_config()
        with patch.object(state.State, "finish", side_effect=OSError(KEY)):
            self.assertEqual(self.run_page()["outcome"], "skipped")
        self.assertEqual(self.run_page()["reason"], "duplicate_page")
        self.assertEqual(self.run_page(URL + "next")["reason"], "budget_exhausted")
        self.assertEqual(len(self.calls), 1)

    def test_publish_recovery_no_new_provider_call(self):
        with patch.object(state.State, "publish", side_effect=OSError(KEY)):
            self.assertEqual(self.run_page()["outcome"], "skipped")
        self.assertEqual(self.run_page()["reason"], "duplicate_page")
        files = list(self.root.glob("data/shadow/llm_extract_*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertEqual(len(files[0].read_text().splitlines()), 1)
        self.assertEqual(len(self.calls), 1)

    def test_missing_corrupt_ledger_and_partial_output_fail_closed(self):
        self.assertEqual(self.run_page()["outcome"], "ok")
        output = next(self.root.glob("data/shadow/llm_extract_*.jsonl"))
        with output.open("a") as f:
            f.write('{"partial":')
        self.assertEqual(self.run_page(URL + "next")["reason"], "partial_shadow_output")
        output.write_text("")
        ledger = self.root / "budget.sqlite3"
        ledger.unlink()
        self.assertEqual(self.run_page(URL + "next")["reason"], "missing_budget_state")
        ledger.write_bytes(b"broken sqlite")
        ledger.chmod(0o600)
        self.assertEqual(self.run_page(URL + "next")["reason"], "shadow_state_error")
        self.assertEqual(len(self.calls), 1)

    def test_backwards_clock_and_expiry(self):
        self.assertEqual(self.run_page()["outcome"], "ok")
        self.clock -= dt.timedelta(seconds=1)
        self.assertEqual(self.run_page(URL + "next")["reason"], "clock_backwards")
        self.clock = dt.datetime(2026, 11, 12, tzinfo=UTC)
        self.assertEqual(self.run_page(URL + "expired")["reason"], "inactive_window")
        self.assertEqual(len(self.calls), 1)

    def test_midnight_dispatch_never_sends_against_wrong_day(self):
        moments = iter([NOW, NOW, NOW, NOW + dt.timedelta(days=1)])
        r = runner.ShadowRunner(self.config_path, clock=lambda: next(moments)).run(
            shadow_root=self.root, page=page(), as_of="2026-11-05", transport=self.transport, key_loader=lambda: KEY)
        self.assertEqual(r["outcome"], "dispatch_clock_changed")
        self.assertFalse(self.calls)

    def test_disabling_during_run_prevents_dispatch(self):
        def clock():
            self.config["enabled"] = False
            self.write_config()
            return NOW
        # First load must see enabled=true; disable at the next clock read.
        calls = iter([NOW, clock])
        def tick():
            value = next(calls)
            return value() if callable(value) else value
        r = runner.ShadowRunner(self.config_path, tick).run(shadow_root=self.root, page=page(),
            as_of="2026-11-05", transport=self.transport, key_loader=lambda: KEY)
        self.assertEqual(r["reason"], "disabled")
        self.assertFalse(self.calls)


    def test_config_changes_after_reservation_stop_dispatch(self):
        original = state.State.reserve
        def reserve(*args):
            result = original(*args)
            self.config["daily_call_cap"] = 1
            self.write_config()
            return result
        with patch.object(state.State, "reserve", side_effect=reserve, autospec=True):
            self.assertEqual(self.run_page()["outcome"], "config_changed")
        self.assertFalse(self.calls)

    def test_output_failure_prevents_reservation_and_provider(self):
        original = state.secure_open
        def protected(path, *args, **kwargs):
            if str(path).endswith(".jsonl"):
                raise OSError(KEY)
            return original(path, *args, **kwargs)
        with patch.object(state, "secure_open", side_effect=protected):
            self.assertEqual(self.run_page()["reason"], "shadow_state_error")
        self.assertFalse(self.calls)
        with state.State(self.root).locked() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)


class SecretAndIsolationTests(Harness):
    def test_env_precedence_and_empty_env_rejected(self):
        self.assertEqual(common.load_key({"DEEPSEEK_API_KEY": KEY}, self.base / "missing"), KEY)
        with self.assertRaises(Stop):
            common.load_key({"DEEPSEEK_API_KEY": ""}, self.base / "missing")

    def test_env_file_is_parsed_never_executed_and_permissions_checked(self):
        p = self.base / "env"
        for text in ("export DEEPSEEK_API_KEY=" + KEY, "DEEPSEEK_API_KEY='" + KEY + "'", 'DEEPSEEK_API_KEY="' + KEY + '"'):
            p.write_text(text + "\nUNRELATED=$(must-never-run)\n")
            p.chmod(0o600)
            self.assertEqual(common.load_key({}, p), KEY)
        for text in ("DEEPSEEK_API_KEY=$(touch " + str(self.base / "executed") + ")", "DEEPSEEK_API_KEY=`id`",
                     "DEEPSEEK_API_KEY=$OTHER", "export DEEPSEEK_API_KEY='safe' 'suffix'",
                     "DEEPSEEK_API_KEY=a\nDEEPSEEK_API_KEY=b"):
            p.write_text(text)
            with self.assertRaises(Stop):
                common.load_key({}, p)
        self.assertFalse((self.base / "executed").exists())
        p.write_text("DEEPSEEK_API_KEY=" + KEY)
        p.chmod(0o644)
        with self.assertRaises(Stop):
            common.load_key({}, p)
        p.chmod(0o600)
        link = self.base / "symlink"
        link.symlink_to(p)
        with self.assertRaises(Stop):
            common.load_key({}, link)

    def test_key_only_in_auth_header_fixed_https_endpoint_no_proxy_redirect_retry(self):
        class Response:
            status = 200
            headers = type("Headers", (), {"get_content_type": lambda _: "application/json"})()
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, limit): return dumps(completion()).encode()
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://bad", "ALL_PROXY": "http://bad"}), patch.object(runner.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = Response()
            raw = runner.provider({"model": "deepseek-flash"}, KEY, 30)
            self.assertEqual(raw["model"], "deepseek-flash")
            args = build.call_args.args
            self.assertEqual(args[0].proxies, {})
            self.assertIs(args[1], runner.NoRedirect)
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, runner.ENDPOINT)
            self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
            self.assertNotIn(KEY.encode(), request.data)
            self.assertIsNone(runner.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.org"))
            build.return_value.open.side_effect = urllib.error.HTTPError(runner.ENDPOINT, 302, KEY, {}, None)
            with self.assertRaisesRegex(Stop, "^provider_error$"):
                runner.provider({}, KEY, 30)
            self.assertEqual(build.return_value.open.call_count, 2)

    def test_no_secret_in_outputs_errors_logs_or_request_prompt(self):
        log = io.StringIO()
        def bad(*args):
            raise RuntimeError(KEY)
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = self.run_page(transport=bad)
        self.assertNotIn(KEY, dumps(result) + log.getvalue())
        self.assertNotIn(KEY.encode(), self.artifacts())
        self.assertEqual(result["outcome"], "provider_error")
        self.assertEqual(result["schema_shape"], {})

    def test_key_echo_in_response_is_rejected(self):
        response = completion()
        response["key"] = KEY
        r = self.run_page(transport=lambda *_: response)
        self.assertEqual(r["outcome"], "credential_in_response")
        self.assertNotIn(KEY.encode(), self.artifacts())

    def test_no_live_db_store_promotion_scheduler_or_runtime_environment(self):
        with patch("pbdata.db.connect", side_effect=AssertionError("live DB")), \
             patch("pbdata.collect.store.insert_raw", side_effect=AssertionError("store")), \
             patch("pbdata.collect.promote.promote_raw", side_effect=AssertionError("promotion")), \
             patch("pbdata.collect.scheduler.enqueue", side_effect=AssertionError("scheduler")), \
             patch.dict(os.environ, {"PB_DATA_ROOT": str(self.base / "live"), "PB_REPO": "/workspace/pitchboard-data"}):
            result = self.run_page()
        self.assertEqual(result["outcome"], "ok")
        self.assertFalse((self.base / "live").exists())
        self.assertEqual(runner.baseline(page().doc(), "2026-11-05"), result["baseline"])
        for p in self.root.rglob("*"):
            self.assertEqual(p.stat().st_mode & 0o077, 0)

    def test_protected_roots_symlink_hardlink_and_unrecognized_root(self):
        for path in common.PROTECTED:
            for p in (path, path / "new-shadow", path.parent):
                with self.subTest(path=p), self.assertRaises(Stop):
                    common.shadow_root(p)
        link = self.base / "link"
        link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(Stop):
            common.shadow_root(link / "shadow")
        self.root.mkdir(mode=0o700)
        (self.root / "other").write_text("untouched")
        self.assertEqual(self.run_page()["reason"], "unrecognized_shadow_root")
        (self.root / "other").unlink()
        self.assertEqual(self.run_page()["outcome"], "ok")
        output = next(self.root.glob("data/shadow/llm_extract_*.jsonl"))
        output.unlink()
        external = self.base / "external"
        external.write_text("safe")
        external.chmod(0o600)
        os.link(external, output)
        self.assertEqual(self.run_page(URL + "next")["reason"], "unsafe_file")
        self.assertEqual(external.read_text(), "safe")


class EvidenceTests(unittest.TestCase):
    def test_valid_evidence_offsets_korean_title_repetition(self):
        p = page()
        result, reasons = schema.validate(proposal(), p.doc(), "2026-11-05")
        self.assertFalse(reasons)
        for field in result.values():
            if field is None:
                continue
            segment = p.doc().segments[field["segment_idx"]]
            self.assertEqual(segment.text[field["start"]:field["end"]], field["quote"])
        text = "한국어 작가 누구나 지원할 수 있습니다."
        doc = Doc("html", segments=[Segment(0, "h", text), Segment(1, "p", text)])
        raw = dict.fromkeys(schema.FIELDS)
        raw["eligibility"] = {"value": {"text": text, "constraints": []}, "quote": text}
        result, _ = schema.validate(raw, doc, "2026-11-05")
        self.assertEqual(result["eligibility"]["occurrence_count"], 2)
        self.assertEqual(result["eligibility"]["segment_idx"], 0)
        doc = parse_html(b"<title>Title evidence</title><p>Body</p>", URL)
        self.assertEqual(doc.segments[0].text, "Title evidence")
        self.assertEqual(doc.text().count("Title evidence"), 1)

    def test_bad_quotes_null_entire_field_leave_others(self):
        for quote in (None, "", "   ", "fabricated", QUOTE.lower(), QUOTE.replace(" ", "  "), "x" * 281,
                      "Writing submissions\n" + QUOTE):
            with self.subTest(quote=quote):
                raw = proposal()
                raw["deadline"]["quote"] = quote
                fields, reasons = schema.validate(raw, page().doc(), "2026-11-05")
                self.assertIsNone(fields["deadline"])
                self.assertIn("deadline", reasons)
                self.assertIsNotNone(fields["pay"])

    def test_contact_details_unsupported_year_invalid_types_and_schema(self):
        raw = proposal()
        raw["deadline"]["value"]["date"] = "2027-12-01"
        result, reasons = schema.validate(raw, page().doc(), "2026-11-05")
        self.assertIsNone(result["deadline"])
        self.assertEqual(reasons["deadline"], "unsupported_year")
        doc = Doc("html", segments=[Segment(0, "p", "Contact editor@example.org for eligibility.")])
        raw = dict.fromkeys(schema.FIELDS)
        raw["eligibility"] = {"value": {"text": "Eligibility", "constraints": []}, "quote": doc.text()}
        self.assertEqual(schema.validate(raw, doc, "2026-11-05")[1]["eligibility"], "evidence_contact_details")
        raw = proposal()
        raw["pay"]["value"]["amount_min"] = 100
        self.assertIsNone(schema.validate(raw, page().doc(), "2026-11-05")[0]["pay"])
        raw = proposal()
        raw["pay"]["value"]["amount_min"] = "200"
        self.assertIsNone(schema.validate(raw, page().doc(), "2026-11-05")[0]["pay"])
        with self.assertRaises(Stop):
            common.loads('{"deadline":null,"deadline":null}')

    def test_not_call_truth_table_closed_and_deadline_conflict(self):
        for status in ("open", "closed", "cancelled", "upcoming", "unknown", "not_call"):
            for real in (True, False):
                self.assertEqual(schema.valid_value("open_call", {"is_real_call": real, "status": status}), real == (status != "not_call"))
        raw = proposal()
        raw["open_call"]["value"] = {"is_real_call": False, "status": "not_call"}
        result, _ = schema.validate(raw, page().doc(), "2026-11-05")
        self.assertIsNone(result["open_call"])
        self.assertIsNotNone(result["pay"])
        raw = proposal()
        result, reasons = schema.validate(raw, page().doc(), "2026-12-02")
        self.assertIsNone(result["open_call"])
        self.assertEqual(reasons["open_call"], "past_deadline_conflict")


class SchemaShapeTests(unittest.TestCase):
    def test_missing_keys_become_null_without_mutating_valid_neighbors(self):
        doc = page().doc()
        expected, _ = schema.validate(proposal(), doc, "2026-11-05")
        cases = [(f,) for f in schema.FIELDS] + [("open_call", "pay", "eligibility"), schema.FIELDS]
        for missing in cases:
            with self.subTest(missing=missing):
                raw = {f: v for f, v in proposal().items() if f not in missing}
                before = copy.deepcopy(raw)
                shape = {"stale": True}
                fields, reasons = schema.validate(raw, doc, "2026-11-05", schema_shape=shape)
                self.assertEqual(raw, before)
                self.assertEqual(fields, {f: None if f in missing else expected[f] for f in schema.FIELDS})
                self.assertEqual(reasons, dict.fromkeys(missing, "missing_key"))
                self.assertEqual(shape, shape_summary(missing=[f for f in schema.FIELDS if f in missing]))
        shape = {"stale": True}
        fields, reasons = schema.validate(dict.fromkeys(schema.FIELDS), doc, "2026-11-05", schema_shape=shape)
        self.assertEqual(fields, dict.fromkeys(schema.FIELDS))
        self.assertEqual(reasons, {})
        self.assertEqual(shape, {})

    def test_missing_keys_preserve_checks_on_remaining_claims(self):
        cases = [
            ({"value": {}, "quote": QUOTE}, "invalid_value"),
            ({**proposal()["pay"], "quote": ""}, "invalid_quote"),
            ({**proposal()["pay"], "quote": "invented"}, "quote_not_found"),
            ({**proposal()["pay"], "quote": "editor@example.org"}, "evidence_contact_details"),
            ({"value": {**proposal()["pay"]["value"], "amount_min": "99"},
              "quote": proposal()["pay"]["quote"]}, "unsupported_amount"),
            ({"value": {**proposal()["pay"]["value"], "text": "invented"},
              "quote": proposal()["pay"]["quote"]}, "value_not_verbatim"),
        ]
        for claim, reason in cases:
            with self.subTest(reason=reason):
                raw = proposal()
                del raw["eligibility"]
                raw["pay"] = claim
                fields, reasons = schema.validate(raw, page().doc(), "2026-11-05")
                self.assertIsNone(fields["pay"])
                self.assertIsNotNone(fields["deadline"])
                self.assertEqual(reasons, {"pay": reason, "eligibility": "missing_key"})

    def test_not_call_nulling_preserves_missing_key_on_absent_fields(self):
        doc = Doc("html", segments=[Segment(0, "p", "Winners announced for the 2026 contest."),
                                    Segment(1, "p", "We pay USD 100 per story.")])
        raw = {**proposal(), "deadline": None, "eligibility": None}
        raw["open_call"] = {"value": {"is_real_call": False, "status": "not_call"},
                            "quote": "Winners announced for the 2026 contest."}
        del raw["submission_fee"]
        shape = {}
        fields, reasons = schema.validate(raw, doc, "2026-11-05", schema_shape=shape)
        self.assertIsNotNone(fields["open_call"])
        for field in schema.FIELDS[1:]:
            self.assertIsNone(fields[field])
        # The nulling loop only touches present claims; absent keys keep missing_key.
        self.assertEqual(reasons, {"pay": "not_call", "submission_fee": "missing_key"})
        self.assertEqual(shape, shape_summary(missing=["submission_fee"]))

    def test_extra_wrapper_and_non_dict_shapes_fail_closed(self):
        cases = [
            ({**proposal(), "extra": "value_sentinel"}, shape_summary(extra=["extra"])),
            ({"extra": "value_sentinel"}, shape_summary(missing=schema.FIELDS, extra=["extra"])),
            ({"result": proposal()}, shape_summary(missing=schema.FIELDS, extra=["result"])),
            # Unsafe-only extras: nothing echoable, so extra stays empty and the
            # invalid-charset names are counted, never stringified.
            ({**proposal(), "Pay": "value_sentinel"}, shape_summary(redacted=1)),
            ({**proposal(), "Pay": "value_sentinel", "대상": "value_sentinel"}, shape_summary(redacted=2)),
        ]
        cases += [(raw, shape_summary(top_type=label)) for raw, label in
                  [("value_sentinel", "str"), (1, "int"), (1.5, "float"),
                   (True, "bool"), (False, "bool"), (None, "null"), (object(), "other")]]
        for raw, expected in cases:
            with self.subTest(top_type=expected["top_type"], extra=expected["extra"],
                              redacted=expected["extra_redacted_count"]):
                shape = {"stale": True}
                with self.assertRaisesRegex(Stop, "^invalid_schema$"):
                    schema.validate(raw, page().doc(), "2026-11-05", schema_shape=shape)
                self.assertEqual(shape, expected)
                self.assertNotIn("value_sentinel", dumps(shape))
                self.assertNotIn(QUOTE, dumps(shape))

    def test_response_object_and_wrapped_object_keep_identical_validation(self):
        config = json.loads(common.CONFIG.read_text())
        doc = page().doc()
        for raw, outcome, expected_shape, expected_reasons in shape_cases():
            if not isinstance(raw, dict):
                continue
            with self.subTest(shape=expected_shape, reasons=expected_reasons):
                results = []
                for wrapped in (False, True):
                    response = completion([raw] if wrapped else raw)
                    before = copy.deepcopy(response)
                    shape = {"stale": True}
                    if outcome == "invalid_schema":
                        with self.assertRaisesRegex(Stop, "^invalid_schema$"):
                            schema.response(response, doc, "2026-11-05", config, schema_shape=shape)
                    else:
                        result = schema.response(response, doc, "2026-11-05", config, schema_shape=shape)
                        self.assertEqual(result[1], expected_reasons)
                        self.assertEqual(result[2], response["usage"])
                        for field in expected_reasons:
                            self.assertIsNone(result[0][field])
                        if raw == proposal():
                            self.assertTrue(all(result[0][f] is not None for f in schema.FIELDS[:-1]))
                        results.append(result)
                        self.assertEqual(schema.response(response, doc, "2026-11-05", config), result)
                    self.assertEqual(shape, {**expected_shape, **({"unwrapped_list": True} if wrapped else {})})
                    self.assertEqual(response, before)
                if results:
                    self.assertEqual(results[0], results[1])

    def test_rejected_lists_report_only_type_and_integer_length(self):
        config = json.loads(common.CONFIG.read_text())
        lists = [raw for raw, outcome, _, _ in shape_cases()
                 if isinstance(raw, list) and outcome == "invalid_schema"]
        # Direct validate() must reject even a singleton dictionary.
        lists += [[proposal()], [{"private_key_sentinel": "private_value_sentinel"}]]
        for raw in lists:
            for direct in (False, True):
                if not direct and len(raw) == 1 and isinstance(raw[0], dict):
                    continue
                with self.subTest(length=len(raw), direct=direct):
                    before = copy.deepcopy(raw)
                    shape = {"stale": True}
                    with self.assertRaisesRegex(Stop, "^invalid_schema$"):
                        if direct:
                            schema.validate(raw, page().doc(), "2026-11-05", schema_shape=shape)
                        else:
                            schema.response(completion(raw), page().doc(), "2026-11-05", config, schema_shape=shape)
                    self.assertEqual(shape, {"top_type": "list", "list_len": len(raw)})
                    self.assertIs(type(shape["list_len"]), int)
                    self.assertEqual(raw, before)

    def test_unwrap_flag_clears_on_later_response(self):
        config = json.loads(common.CONFIG.read_text())
        shape = {}
        for content, expected in [([proposal()], {"unwrapped_list": True}), (proposal(), {}),
                                  ([proposal()], {"unwrapped_list": True}),
                                  ([], {"top_type": "list", "list_len": 0})]:
            with self.subTest(expected=expected):
                if content == []:
                    with self.assertRaisesRegex(Stop, "^invalid_schema$"):
                        schema.response(completion(content), page().doc(), "2026-11-05", config, schema_shape=shape)
                else:
                    schema.response(completion(content), page().doc(), "2026-11-05", config, schema_shape=shape)
                self.assertEqual(shape, expected)

    def test_wrapped_content_does_not_bypass_response_guards(self):
        config = json.loads(common.CONFIG.read_text())
        cases = []
        for key, value, code in [
                ("model", "other", "model_mismatch"), ("usage", {}, "invalid_usage"),
                ("usage", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 1}, "usage_invariant")]:
            cases.append(({**completion([proposal()]), key: value}, code))
        for finish in ("length", "tool_calls", "content_filter"):
            raw = completion([proposal()])
            raw["choices"][0]["finish_reason"] = finish
            cases.append((raw, "invalid_completion"))
        for key, value in (("refusal", "no"), ("tool_calls", [{}]), ("reasoning_content", "thinking")):
            raw = completion([proposal()])
            raw["choices"][0]["message"][key] = value
            cases.append((raw, "invalid_completion"))
        raw = completion([proposal()])
        raw["usage"]["completion_tokens_details"] = {"reasoning_tokens": 1}
        cases.append((raw, "usage_invariant"))
        for provider in ("deepseek", "qwen"):
            for raw, code in cases:
                with self.subTest(provider=provider, code=code):
                    raw = copy.deepcopy(raw)
                    if provider == "qwen" and code != "model_mismatch":
                        raw["model"] = schema.QWEN_MODEL
                    shape = {"unwrapped_list": True}
                    with self.assertRaisesRegex(Stop, "^" + code + "$"), \
                         patch.object(schema, "validate", side_effect=AssertionError("validation reached")):
                        schema.response(raw, page().doc(), "2026-11-05", config, provider, schema_shape=shape)
                    self.assertEqual(shape, {})

    def test_names_are_filtered_before_sorting_and_bounded_without_values(self):
        safe = ["name_" + c for c in "abcdefghijklm"] + ["x" * 40, "zeta"]
        unsafe = ["editor@example.org", "+82 10 1234 5678", "punctuation-key", "대상", "name\n",
                  "", "x" * 41, 17, None]
        raw = dict.fromkeys(reversed(safe + unsafe), {"nested": QUOTE, "value": "looks_like_key"})
        before = copy.deepcopy(raw)
        shape = {}
        with self.assertRaisesRegex(Stop, "^invalid_schema$"):
            schema.validate(raw, page().doc(), "2026-11-05", schema_shape=shape)
        self.assertEqual(raw, before)
        self.assertEqual(shape, shape_summary(missing=schema.FIELDS, extra=safe[:5], redacted=9, omitted=10))
        self.assertLessEqual(len(shape["missing"]) + len(shape["extra"]), 10)
        for value in (QUOTE, "nested", "looks_like_key"):
            self.assertNotIn(value, dumps(shape))
        # The 40-character boundary is accepted; 41 characters and a trailing newline are counted only.
        raw = {**dict.fromkeys(schema.FIELDS), "x" * 40: "value", "x" * 41: "value", "name\n": "value"}
        with self.assertRaisesRegex(Stop, "^invalid_schema$"):
            schema.validate(raw, page().doc(), "2026-11-05", schema_shape=shape)
        self.assertEqual(shape, shape_summary(extra=["x" * 40], redacted=2))
        raw = {**dict.fromkeys(schema.FIELDS), **dict.fromkeys(reversed(safe), "value")}
        with self.assertRaisesRegex(Stop, "^invalid_schema$"):
            schema.validate(raw, page().doc(), "2026-11-05", schema_shape=shape)
        self.assertEqual(shape, shape_summary(extra=safe[:10], omitted=5))

    def test_response_clears_reused_shape_before_prevalidation_failures(self):
        config = json.loads(common.CONFIG.read_text())
        cases = [(None, "model_mismatch")]
        for change, code in [({"model": "other"}, "model_mismatch"), ({"usage": {}}, "invalid_usage"),
                             ({"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 1}}, "usage_invariant"),
                             ({"choices": []}, "invalid_completion")]:
            cases.append(({**completion(), **change}, code))
        raw = completion()
        raw["choices"][0]["message"]["content"] = "invalid JSON"
        cases.append((raw, "invalid_model_json"))
        for raw, code in cases:
            with self.subTest(code=code):
                shape = {"stale": True}
                with self.assertRaisesRegex(Stop, "^" + code + "$"):
                    schema.response(raw, page().doc(), "2026-11-05", config, schema_shape=shape)
                self.assertEqual(shape, {})

    def test_hash_distinguishes_shape_behavior_without_changing_prompt(self):
        old_hash = digest(dumps({"version": schema.VERSION, "evidence_version": "verbatim-2.1",
                                 "value_keys": {k: sorted(v) for k, v in schema.VALUE_KEYS.items()}}))
        self.assertNotEqual(schema.SCHEMA_HASH, old_hash)
        self.assertEqual(schema.PROMPT_HASH, "c82237cc2dafc1ba7c05826f2d9fd78e172f7bf185f05aaecbc82045a4f31331")
        self.assertEqual(schema.VERSION, "pb-shadow-2")

    def test_hash_distinguishes_list_unwrapping_from_previous_shape_version(self):
        old_hash = digest(dumps({"version": schema.VERSION, "evidence_version": "verbatim-2.1",
                                 "shape_version": "missing-null-1",
                                 "value_keys": {k: sorted(v) for k, v in schema.VALUE_KEYS.items()}}))
        self.assertEqual(old_hash, "9c9d1f4c8e8759ef64e6a3060234d5978d8bcc553d9bd93b9f3b9e530661b7d3")
        self.assertNotEqual(schema.SCHEMA_HASH, old_hash)


class SchemaShapeRunnerTests(Harness):
    def test_primary_shapes_reach_jsonl_and_ledger_without_fallback(self):
        for i, (raw, outcome, shape, reasons) in enumerate(shape_cases()):
            with self.subTest(case=i):
                self.root = self.base / ("shape" + str(i))
                transport = Mock(return_value=completion(raw))
                with patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
                    result = self.run_page(transport=transport,
                                           fallback_transport=lambda *_: self.fail("unexpected fallback"),
                                           fallback_key_loader=lambda: self.fail("unexpected fallback key lookup"))
                repaired = outcome == "invalid_schema" and (bool(shape.get("missing")) or shape.get("list_len", 1) != 1)
                cost = "0.6049152" if repaired else "0.3024576"
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["schema_shape"], shape)
                self.assertEqual(result["validation_reasons"], reasons)
                self.assertEqual(result["provider_calls"], 1 + repaired)
                self.assertEqual(transport.call_count, 1 + repaired)
                self.assertIs(result["repair_attempted"], repaired)
                self.assertEqual(result["repair_outcome"], "invalid_schema" if repaired else None)
                self.assertEqual(result["reserved_usd"], cost)
                self.assertEqual(result["charged_ceiling_usd"], cost)
                self.assertEqual(result["provider"], "deepseek")
                self.assertIsNone(result["fallback_from"])
                self.assertEqual(result["schema_sha256"], schema.SCHEMA_HASH)
                self.assertEqual(result["parser_version"], "parse-1")
                if outcome == "invalid_schema":
                    self.assertIsNone(result["fields"])
                else:
                    self.assertEqual(set(result["fields"]), set(schema.FIELDS))
                    for field in reasons:
                        self.assertIsNone(result["fields"][field])
                self.assert_persisted_record(result)
                self.assertNotIn(KEY.encode(), self.artifacts())

    def test_credential_in_permitted_key_is_rejected_before_shape_logging(self):
        secret = "sk_sentinel_never_log"
        fields = {**proposal(), secret: "value"}
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                self.root = self.base / str(wrapped)
                raw = completion([fields] if wrapped else fields)
                with patch.object(schema, "validate", side_effect=AssertionError("validation reached")):
                    result = self.run_page(transport=lambda *_: raw, key_loader=lambda: secret)
                self.assertEqual(result["outcome"], "credential_in_response")
                self.assertEqual(result["schema_shape"], {})
                self.assertIs(result["repair_attempted"], False)
                self.assertIsNone(result["repair_outcome"])
                self.assertNotIn(secret, dumps(result))
                self.assertNotIn(secret.encode(), self.artifacts())
                self.assert_persisted_record(result)

    def test_credential_matching_generated_metadata_discards_record(self):
        secret = "extra_omitted_count"
        raw = proposal()
        del raw["pay"]
        result = self.run_page(transport=lambda *_: completion(raw), key_loader=lambda: secret)
        self.assertEqual(result, {"outcome": "skipped", "reason": "credential_in_record"})
        self.assertNotIn(secret.encode(), self.artifacts())
        self.assertFalse(any(p.read_text() for p in self.root.glob("data/shadow/llm_extract_*.jsonl")))
        with state.State(self.root).locked() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT result FROM attempts")], [None])


class SchemaRepairTests(Harness):
    def test_primary_repair_validates_evidence_and_publishes_once(self):
        self.config["daily_call_cap"] = 2
        self.write_config()
        prior = [{"result": "previous_value_sentinel"}]
        repaired = proposal()
        repaired["eligibility"]["quote"] = "All writers anywhere may apply."
        final = completion([repaired])
        final["model"] = "DeepSeek-V4.1-Flash-1017"
        final["usage"].update(prompt_tokens=200, total_tokens=300)
        source = page(body=HTML.replace(b"</html>", b"<p>We pay USD 200 per story.</p></html>"))
        transport = Mock(side_effect=[completion(prior), final])
        fallback = Mock()
        fallback_key = Mock(return_value="qwen-test-key")
        result = self.run_page(page=source, transport=transport, fallback_transport=fallback,
                               fallback_key_loader=fallback_key)

        self.assertEqual(result["outcome"], "ok")
        self.assertIs(result["repair_attempted"], True)
        self.assertEqual(result["repair_outcome"], "ok")
        self.assertEqual(result["provider_calls"], 2)
        self.assertEqual(result["provider"], "deepseek")
        self.assertIsNone(result["fallback_from"])
        self.assertEqual(result["returned_model"], final["model"])
        self.assertEqual(result["usage"], final["usage"])
        self.assertEqual(result["reserved_usd"], "0.6049152")
        self.assertEqual(result["charged_ceiling_usd"], "0.6049152")
        self.assertEqual(result["schema_shape"], {**shape_summary(missing=schema.FIELDS, extra=["result"]),
                                                  "unwrapped_list": True})
        self.assertEqual(result["fields"]["open_call"]["quote"], QUOTE)
        self.assertEqual(result["fields"]["pay"]["value"], repaired["pay"]["value"])
        self.assertEqual(result["validation_reasons"], {"eligibility": "quote_not_found"})
        self.assertIsNone(result["fields"]["eligibility"])
        self.assertIn(("compensation.rate", ["ambiguous_rules"]),
                      [(row["predicate"], row["reasons"]) for row in result["reviews"]])
        self.assertEqual(transport.call_count, 2)
        fallback.assert_not_called()
        fallback_key.assert_not_called()
        first_body, key, timeout = transport.call_args_list[0].args
        repair_request, repair_key, repair_timeout = transport.call_args_list[1].args
        self.assertEqual((repair_key, repair_timeout), (key, timeout))
        self.assertEqual({**repair_request, "messages": repair_request["messages"][:-1]}, first_body)
        self.assertEqual(repair_request["messages"][-1]["role"], "user")
        self.assertEqual(repair_request["messages"][-1]["content"].splitlines()[-1], dumps(prior))
        self.assert_persisted_record(result)
        self.assertNotIn(b"previous_value_sentinel", self.artifacts())
        self.assertEqual(self.run_page(URL + "-next")["reason"], "budget_exhausted")

    def test_repair_requires_all_keys_and_never_retries_again(self):
        transport = Mock(side_effect=[completion({"extra": 1}), completion({"open_call": None})])
        result = self.run_page(transport=transport)
        self.assertEqual(result["outcome"], "invalid_schema")
        self.assertIs(result["repair_attempted"], True)
        self.assertEqual(result["repair_outcome"], "invalid_schema")
        self.assertEqual(result["schema_shape"], shape_summary(missing=schema.FIELDS, extra=["extra"]))
        self.assertEqual(result["provider_calls"], 2)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(result["charged_ceiling_usd"], "0.6049152")
        self.assertIsNone(result["fields"])
        self.assert_persisted_record(result)

    def test_repair_keeps_safe_failure_codes_and_never_falls_back(self):
        cases = [(TimeoutError("private transport detail"), "provider_error", "provider_error"),
                 (Stop("duplicate_json_key"), "duplicate_json_key", "provider_error"),
                 ({**completion(), "model": "wrong-model"}, "model_mismatch", "provider_error"),
                 ({**completion(), "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3}},
                  "usage_invariant", "provider_error"),
                 (completion({"note": KEY}), "credential_in_response", "provider_error")]
        for content, code in (("not JSON", "invalid_model_json"), ('{"a":null,"a":null}', "duplicate_json_key"),
                              ('{"a":NaN}', "invalid_json_number")):
            raw = completion()
            raw["choices"][0]["message"]["content"] = content
            cases.append((raw, code, "invalid_schema"))
        for i, (raw, outcome, repair_outcome) in enumerate(cases):
            with self.subTest(case=i):
                self.root = self.base / str(i)
                transport = Mock(side_effect=[completion({"extra": 1}), raw])
                fallback = Mock()
                fallback_key = Mock(return_value="qwen-test-key")
                result = self.run_page(transport=transport, fallback_transport=fallback,
                                       fallback_key_loader=fallback_key)
                self.assertEqual(result["outcome"], outcome)
                self.assertIs(result["repair_attempted"], True)
                self.assertEqual(result["repair_outcome"], repair_outcome)
                self.assertEqual(result["provider_calls"], 2)
                self.assertEqual(transport.call_count, 2)
                fallback.assert_not_called()
                fallback_key.assert_not_called()
                self.assertIsNone(result["fallback_from"])
                self.assert_persisted_record(result)
                self.assertNotIn(b"private transport detail", self.artifacts())
                self.assertNotIn(KEY.encode(), self.artifacts())
                if outcome in {"model_mismatch", "usage_invariant"}:
                    self.assertEqual(self.run_page(URL + "-next")["reason"], "experiment_halted")

    def test_call_and_cost_caps_skip_repair_without_charging(self):
        for cap, value in (("daily_call_cap", 1), ("daily_cost_cap_usd", "0.3024576")):
            with self.subTest(cap=cap), patch.dict(self.config, {cap: value}):
                self.root = self.base / cap
                self.write_config()
                transport = Mock(return_value=completion({"extra": 1}))
                result = self.run_page(transport=transport)
                self.assertEqual(result["outcome"], "invalid_schema")
                self.assertIs(result["repair_attempted"], False)
                self.assertEqual(result["repair_outcome"], "skipped_cap")
                self.assertEqual(result["provider_calls"], 1)
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(result["reserved_usd"], "0.3024576")
                self.assertEqual(result["charged_ceiling_usd"], "0.3024576")
                self.assert_persisted_record(result)

    def test_guards_recheck_config_and_dispatch_day_before_charging(self):
        def disable():
            self.config["enabled"] = False
            self.write_config()

        def next_day():
            self.clock += dt.timedelta(days=1)

        for guard in (disable, next_day):
            with self.subTest(guard=guard.__name__):
                self.root = self.base / guard.__name__
                self.config["enabled"] = True
                self.write_config()

                def transport(*args):
                    self.calls.append(args)
                    guard()
                    return completion({"extra": 1})

                self.calls.clear()
                result = self.run_page(transport=transport)
                self.assertEqual(result["outcome"], "invalid_schema")
                self.assertIs(result["repair_attempted"], False)
                self.assertIsNone(result["repair_outcome"])
                self.assertEqual(result["provider_calls"], 1)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(result["reserved_usd"], "0.3024576")
                self.assert_persisted_record(result)

    def test_escaped_credential_is_rejected_before_building_correction(self):
        raw = completion({"invented": KEY})
        raw["choices"][0]["message"]["content"] = dumps({"invented": KEY}).replace(KEY, r"\u0073" + KEY[1:])
        self.assertNotIn(KEY, dumps(raw))
        transport = Mock(return_value=raw)
        with patch.object(runner, "repair_body", wraps=schema.repair_body) as repair_body:
            result = self.run_page(transport=transport)
        repair_body.assert_not_called()
        self.assertEqual(result["outcome"], "credential_in_response")
        self.assertIs(result["repair_attempted"], False)
        self.assertIsNone(result["repair_outcome"])
        self.assertEqual(result["provider_calls"], 1)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(result["reserved_usd"], "0.3024576")
        self.assert_persisted_record(result)
        self.assertNotIn(KEY.encode(), self.artifacts())

    def test_generated_correction_is_checked_for_credentials(self):
        key = "skeleton"
        transport = Mock(return_value=completion({"extra": 1}))
        result = self.run_page(transport=transport, key_loader=lambda: key)
        self.assertEqual(result["outcome"], "credential_in_response")
        self.assertIs(result["repair_attempted"], False)
        self.assertIsNone(result["repair_outcome"])
        self.assertEqual(result["provider_calls"], 1)
        self.assertEqual(transport.call_count, 1)
        self.assert_persisted_record(result)
        self.assertNotIn(key.encode(), self.artifacts())

    def test_listing_repair_requires_one_object_and_bounds_utf8_excerpt(self):
        prior = [{"listing": "가" * 1000}] * 14
        transport = Mock(side_effect=[completion(prior), completion(dict.fromkeys(schema.FIELDS))])
        result = self.run_page(transport=transport)
        self.assertEqual(result["schema_shape"], {"top_type": "list", "list_len": 14})
        self.assertEqual(result["repair_outcome"], "ok")
        self.assertIsNone(result["fields"]["open_call"])
        self.assertEqual(transport.call_count, 2)
        correction = transport.call_args.args[0]["messages"][-1]["content"]
        self.assertIn("a single JSON object only", correction)
        self.assertIn("a single object describing the page's one call", correction)
        self.assertIn("If the page lists several calls, set open_call and all other call-specific fields to null", correction)
        self.assertIn("Required top-level keys: " + ", ".join(schema.FIELDS), correction)
        self.assertIn("JSON skeleton: " + dumps(dict.fromkeys(schema.FIELDS)), correction)
        self.assertIn("untrusted data", correction)
        excerpt, marker = correction.splitlines()[-2:]
        excerpt_bytes = excerpt.encode("utf-8")
        self.assertTrue(2045 < len(excerpt_bytes) <= 2048)
        self.assertTrue(dumps(prior).encode("utf-8").startswith(excerpt_bytes))
        self.assertEqual(marker, "[previous JSON truncated]")
        self.assert_persisted_record(result)


class ProviderFailures(Harness):
    def test_invalid_provider_responses_count_and_keep_cost(self):
        cases = []
        for key, value in (("model", "other-model"), ("usage", None), ("choices", [])):
            r = completion(); r[key] = value; cases.append(r)
        for content in ("", "not json", '{"a":null,"a":null}', '{"extra":1}', '[]'):
            r = completion(); r["choices"][0]["message"]["content"] = content; cases.append(r)
        for finish in ("length", "tool_calls", "content_filter"):
            r = completion(); r["choices"][0]["finish_reason"] = finish; cases.append(r)
        for k, v in (("refusal", "no"), ("tool_calls", [{}]), ("reasoning_content", "thinking")):
            r = completion(); r["choices"][0]["message"][k] = v; cases.append(r)
        r = completion(); r["usage"]["completion_tokens"] = True; cases.append(r)
        r = completion(); r["usage"]["completion_tokens_details"] = {"reasoning_tokens": 50}; cases.append(r)
        for i, raw in enumerate(cases):
            with self.subTest(i=i):
                self.root = self.base / ("failure" + str(i))
                transport = Mock(return_value=raw)
                result = self.run_page(transport=transport)
                repaired = bool(raw["choices"] and raw["choices"][0]["message"]["content"] in ('{"extra":1}', '[]'))
                self.assertNotEqual(result["outcome"], "ok")
                self.assertEqual(result["charged_ceiling_usd"], "0.6049152" if repaired else "0.3024576")
                self.assertEqual(result["provider_calls"], 1 + repaired)
                self.assertEqual(transport.call_count, 1 + repaired)
                self.assertIs(result["repair_attempted"], repaired)
                self.assertEqual(result["repair_outcome"], "invalid_schema" if repaired else None)
                self.assertIsNone(result["fields"])
                self.assertEqual(self.run_page()["reason"], "experiment_halted" if result["outcome"] in ("model_mismatch", "usage_invariant") else "duplicate_page")


class PolicyTests(Harness):
    def fake_fetcher(self, responses):
        f = policy.PolicyFetcher(clock=lambda: 100, sleep=lambda _: None)
        self.requests = []
        def get(url, accept, limit, delay=2):
            canonical = policy.canonical_url(url)
            self.requests.append(canonical)
            response = responses[canonical]
            if isinstance(response, Exception):
                raise response
            return response
        f._get = get
        return f

    def test_instagram_and_blocked_domains_never_dns_fetch_or_llm(self):
        hosts = ["instagram.com", "WWW.INSTAGRAM.COM", "api.instagram.com.", "instagram.com:443", "instagram.com:8443",
                 "user@instagram.com", "instagram%2ecom", "instagram.com\\@example.org", "instagram.com\n", "facebook.com", "bostonreview.net"]
        for host in hosts:
            url = "https://" + host + "/call"
            with self.subTest(host=host), patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")), patch.object(policy.PolicyFetcher, "_get", side_effect=AssertionError("HTTP")):
                r = self.run_page(page=None, url=url)
                self.assertEqual(r["outcome"], "skipped")
                r = self.run_page(page=page(url))
                self.assertEqual(r["outcome"], "skipped")
        self.assertFalse(self.calls)
        self.assertEqual(policy.canonical_url("https://notinstagram.com/", blocked=set()), "https://notinstagram.com/")
        with self.assertRaisesRegex(Stop, "blocked_host"):
            policy.canonical_url("https://instagram.com/", blocked=set())

    def test_robots_status_table_and_denial_before_page_or_model(self):
        robots = "https://example.org/robots.txt"
        cases = [(200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /call")]
        cases += [(s, {}, b"") for s in (301, 302, 307, 308, 401, 403, 429, 500, 503, 204)]
        cases += [(200, {"Content-Type": c}, b"User-agent: *\nAllow: /") for c in ("text/html", "application/json")]
        cases += [(200, {"Content-Type": "text/plain"}, text) for text in (b"", b"<html>bad</html>", b"User-agent: *\xff", b"nonsense")]
        for response in cases + [Stop("fetch_error")]:
            with self.subTest(response=response):
                f = self.fake_fetcher({robots: response})
                result = self.run_page(page=None, url=URL, fetcher=f)
                self.assertEqual(result["outcome"], "skipped")
                self.assertEqual(self.requests, [robots])
        self.assertFalse(self.calls)

    def test_robots_allow_404_410_cache_and_live_result(self):
        for status in (200, 404, 410):
            f = self.fake_fetcher({"https://example.org/robots.txt": (status, {"Content-Type": "text/plain"}, b"User-agent: *\nAllow: /"),
                                   URL: (200, {"Content-Type": "text/html"}, HTML)})
            fetched = f.fetch(URL)
            self.assertEqual(fetched.provenance, "live_verified")
            f.fetch(URL)
            self.assertEqual(self.requests.count("https://example.org/robots.txt"), 1)
        self.assertEqual(self.run_page(page=None, url=URL, fetcher=f)["outcome"], "ok")

    def test_redirect_blocks_instagram_before_request_and_checks_new_origin_robots(self):
        f = self.fake_fetcher({"https://example.org/robots.txt": (404, {}, b""),
                               URL: (302, {"Location": "https://instagram.com/hidden"}, b"")})
        self.assertEqual(self.run_page(page=None, url=URL, fetcher=f)["reason"], "blocked_host")
        self.assertEqual(len(self.requests), 2)
        f = self.fake_fetcher({"https://example.org/robots.txt": (404, {}, b""),
                               URL: (302, {"Location": "https://other.org/call"}, b""),
                               "https://other.org/robots.txt": (403, {}, b"")})
        self.assertEqual(self.run_page(page=None, url=URL, fetcher=f)["reason"], "denied_robots")
        self.assertEqual(self.requests[-1], "https://other.org/robots.txt")
        self.assertFalse(self.calls)

    def test_page_denials_challenges_login_compression_oversize_no_bypass(self):
        for status in (301, 401, 402, 403, 429, 451, 500):
            f = self.fake_fetcher({"https://example.org/robots.txt": (404, {}, b""), URL: (status, {}, b"")})
            self.assertEqual(self.run_page(page=None, url=URL, fetcher=f)["outcome"], "skipped")
        for data in (b"<title>Sign in</title>", b"<h1>verify you are human</h1>", b'<input type="password">', b"<p>Please log in to continue reading.</p>", b"<p>Members only</p>"):
            self.assertEqual(self.run_page(page=page(body=data))["reason"], "access_wall")
        self.assertEqual(self.run_page(page=page(body=b"<p>" + b"x" * 25000 + b"</p>"))["reason"], "page_too_large")
        self.assertFalse(self.calls)

    def _answers(self, *ips):
        infos = []
        for ip in ips:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
            infos.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return infos

    def test_direct_and_documented_gateway_paths_pin_one_route(self):
        env = {"PITCHBOARD_EGRESS_GATEWAY_CIDRS": "10.0.0.0/8,198.18.0.0/15,169.254.0.0/16",
               "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9"}
        for ip in ("1.1.1.1", "198.17.255.255"):
            with self.subTest(route="direct", ip=ip), patch.dict(os.environ, env), patch.object(socket, "getaddrinfo", return_value=self._answers(ip)) as resolve, patch.object(socket, "create_connection") as connect:
                policy.public_socket("example.org", 443, 15, egress="direct")
                policy.public_socket("example.org", 443, 15, egress="sandbox")
                self.assertEqual(resolve.call_count, 2)
                self.assertEqual(connect.call_args_list[0].args[0], (ip, 443))
                self.assertEqual(connect.call_args_list[1].args[0], (ip, 443))
        for ip in ("198.18.0.1", "198.19.255.255", "::ffff:198.18.0.1"):
            with self.subTest(route="gateway", ip=ip), patch.dict(os.environ, env), patch.object(socket, "getaddrinfo", return_value=self._answers(ip)), patch.object(socket, "create_connection") as connect:
                with self.assertRaisesRegex(Stop, "blocked_network"):
                    policy.public_socket("example.org", 443, 15)
                connect.assert_not_called()
                policy.public_socket("example.org", 443, 15, egress="sandbox")
                self.assertEqual(connect.call_args.args[0], (ip, 443))
        with patch.dict(os.environ, env), patch.object(socket, "getaddrinfo", return_value=self._answers("198.18.0.1", "198.19.0.8")), patch.object(socket, "create_connection") as connect:
            with self.assertRaisesRegex(Stop, "blocked_network"):
                policy.public_socket("example.org", 443, 15, egress="direct")
            policy.public_socket("example.org", 443, 15, egress="sandbox")
            self.assertEqual(connect.call_args.args[0], ("198.18.0.1", 443))
        with patch.dict(os.environ, env):
            fetcher = policy.PolicyFetcher(sleep=lambda *_: None, egress="sandbox")
            # An empty ProxyHandler suppresses the env-proxy default and adds no proxy method.
            self.assertEqual(fetcher.egress, "sandbox")
            self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) for h in fetcher.opener.handlers))
            self.assertFalse(any(getattr(h, "proxies", None) for h in fetcher.opener.handlers))
            self.assertIn(policy.PublicHTTPSHandler, [type(h) for h in fetcher.opener.handle_open["https"]])
            self.assertNotIn(urllib.request.ProxyHandler, [type(h) for h in fetcher.opener.handle_open["https"]])
            https = next(h for h in fetcher.opener.handlers if isinstance(h, policy.PublicHTTPSHandler))
            self.assertEqual(https._connection("example.org", 443).egress, "sandbox")
            direct = policy.PolicyFetcher(sleep=lambda *_: None)
            direct_https = next(h for h in direct.opener.handlers if isinstance(h, policy.PublicHTTPSHandler))
            self.assertEqual(direct_https._connection("example.org", 443).egress, "direct")
        with self.assertRaisesRegex(Stop, "invalid_config"):
            policy.PolicyFetcher(egress="10.0.0.0/8")

    def test_runner_passes_sandbox_egress_to_default_fetcher(self):
        self.config["egress"] = "sandbox"
        self.write_config()
        seen = {}
        class Recorder(policy.PolicyFetcher):
            def __init__(self, *args, **kwargs):
                seen["egress"] = kwargs.get("egress", "direct")
                super().__init__(*args, **kwargs)
            def fetch(self, url):
                return page(url)
        with patch.object(runner, "PolicyFetcher", Recorder):
            self.assertEqual(self.run_page(page=None)["outcome"], "ok")
        self.assertEqual(seen["egress"], "sandbox")

    def test_private_metadata_mixed_and_literal_gateway_do_not_connect(self):
        denied = (
            ("10.0.0.1",), ("192.168.0.1",), ("172.16.0.1",), ("127.0.0.1",), ("169.254.169.254",),
            ("::1",), ("::ffff:169.254.169.254",), ("100.64.0.1",), ("0.0.0.0",), ("192.0.2.1",),
            ("198.18.0.1", "10.0.0.1"), ("198.18.0.1", "169.254.169.254"), ("198.18.0.1", "1.1.1.1"),
        )
        env = {"PITCHBOARD_EGRESS_GATEWAY_CIDRS": "10.0.0.0/8,198.18.0.0/15,169.254.0.0/16"}
        for ips in denied:
            with self.subTest(ips=ips), patch.dict(os.environ, env), patch.object(socket, "getaddrinfo", return_value=self._answers(*ips)), patch.object(socket, "create_connection") as connect:
                with self.assertRaisesRegex(Stop, "blocked_network"):
                    policy.public_socket("example.org", 443, 15)
                connect.assert_not_called()
        for ips in (("10.0.0.1",), ("169.254.169.254",), ("198.18.0.1", "10.0.0.1"), ("198.18.0.1", "1.1.1.1")):
            with self.subTest(sandbox=ips), patch.dict(os.environ, env), patch.object(socket, "getaddrinfo", return_value=self._answers(*ips)), patch.object(socket, "create_connection") as connect:
                with self.assertRaisesRegex(Stop, "blocked_network"):
                    policy.public_socket("example.org", 443, 15, egress="sandbox")
                connect.assert_not_called()
        with patch.object(socket, "getaddrinfo", return_value=[]), patch.object(socket, "create_connection") as connect:
            with self.assertRaisesRegex(Stop, "blocked_network"):
                policy.public_socket("example.org", 443, 15)
            connect.assert_not_called()
        for host in ("198.18.0.1", "10.0.0.1", "169.254.169.254", "127.0.0.1"):
            with self.subTest(literal=host), patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")), patch.object(socket, "create_connection", side_effect=AssertionError("connect")):
                with self.assertRaisesRegex(Stop, "blocked_network"):
                    policy.public_socket(host, 443, 15)
        with patch.object(socket, "getaddrinfo", return_value=self._answers("198.18.0.1")), patch.object(socket, "create_connection") as connect:
            with self.assertRaisesRegex(Stop, "blocked_network"):
                policy.public_socket("1.1.1.1", 443, 15)
            connect.assert_not_called()
        with patch.object(socket, "getaddrinfo", return_value=self._answers("1.1.1.1")), patch.object(socket, "create_connection") as connect:
            policy.public_socket("1.1.1.1", 443, 15)
            self.assertEqual(connect.call_args.args[0], ("1.1.1.1", 443))

    def test_malicious_hosts_and_redirects_never_resolve(self):
        urls = (
            "https://169.254.169.254/latest/meta-data", "https://198.18.0.1/", "https://10.0.0.1/",
            "https://127.0.0.1/", "https://localhost/admin", "https://metadata.google.internal/computeMetadata/v1/",
            "https://metadata.goog/", "https://printer.local/", "https://intranet.internal/",
            "https://files.localdomain/", "https://0177.0.0.1/", "https://0x7f000001/",
            "https://user:pass@example.org/secret", "http://198.18.0.1:443/",
        )
        for url in urls:
            with self.subTest(url=url), patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")), patch.object(socket, "create_connection", side_effect=AssertionError("connect")):
                result = self.run_page(page=None, url=url)
                self.assertEqual(result["outcome"], "skipped")
                self.assertIn(result["reason"], {"blocked_network", "bad_url"})
                with self.assertRaises(Stop):
                    policy.canonical_url(url)
        for loc in ("https://169.254.169.254/", "https://198.18.0.1/x", "https://10.1.1.1/",
                    "https://metadata.google.internal/", "https://user:pass@example.org/a", "https://intranet.internal/a"):
            with self.subTest(redirect=loc), patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")):
                f = self.fake_fetcher({"https://example.org/robots.txt": (404, {}, b""),
                                       URL: (302, {"Location": loc}, b"")})
                result = self.run_page(page=None, url=URL, fetcher=f)
                self.assertEqual(result["outcome"], "skipped")
                self.assertFalse(any(piece in " ".join(self.requests) for piece in ("169.254.169.254", "198.18.0.1", "10.1.1.1", "internal", "user:pass")))
        def resolve(host, port, *args, **kwargs):
            if host == "evil.example":
                return self._answers("10.0.0.1")
            if host == "example.org":
                return self._answers("198.18.0.1")
            raise AssertionError(host)
        with patch.object(socket, "getaddrinfo", side_effect=resolve), patch.object(socket, "create_connection") as connect:
            with self.assertRaisesRegex(Stop, "blocked_network"):
                policy.public_socket("example.org", 443, 15, egress="direct")
            connect.assert_not_called()
            policy.public_socket("example.org", 443, 15, egress="sandbox")
            self.assertEqual(connect.call_args.args[0], ("198.18.0.1", 443))
            connect.reset_mock()
            with self.assertRaisesRegex(Stop, "blocked_network"):
                policy.public_socket("evil.example", 443, 15, egress="sandbox")
            connect.assert_not_called()
        self.assertFalse(self.calls)

    def test_transport_caps_and_nontext_compressed_robots(self):
        class Response:
            code = 200
            headers = {"Content-Type": "text/plain"}
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, n): return b"x" * n
        f = policy.PolicyFetcher(sleep=lambda _: None)
        with patch.object(f.opener, "open", return_value=Response()):
            with self.assertRaisesRegex(Stop, "invalid_transport_body"):
                f._get("https://example.org/robots.txt", "text/plain", 500000)
        Response.read = lambda self, n: b"User-agent: *\nAllow: /"
        Response.headers = {"Content-Type": "text/plain", "Content-Encoding": "gzip"}
        with patch.object(f.opener, "open", return_value=Response()):
            with self.assertRaisesRegex(Stop, "invalid_transport_body"):
                f._get("https://example.org/robots.txt", "text/plain", 500000)

    def test_cached_policy_envelope_rechecked(self):
        invalid = policy.Page(URL, URL, HTML, ("https://instagram.com/x", URL), "offline_cached")
        self.assertEqual(self.run_page(page=invalid)["reason"], "blocked_host")
        invalid = policy.Page(URL, URL, HTML, (URL,), "live_verified")
        self.assertEqual(self.run_page(page=invalid)["reason"], "invalid_policy_envelope")
