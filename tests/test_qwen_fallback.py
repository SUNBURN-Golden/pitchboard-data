"""Qwen is used only after a DeepSeek provider failure. All HTTP is injected; no live calls or secrets."""
import json
import unittest
from unittest.mock import patch
from pbdata.collect.shadow import common, runner, schema
from pbdata.collect.shadow.common import Stop, dumps

from test_llm_extract import URL, Harness, completion


QWEN_KEY = "qwen-secret-never-log"
QWEN = schema.QWEN_MODEL


def qwen_completion(fields=None):
    raw = completion(fields)
    raw["model"] = QWEN
    return raw


class QwenFallbackTests(Harness):
    def fallback(self, url=URL, deepseek=None, qwen=None, **kwargs):
        def fail(*_):
            self.calls.append(("deepseek",))
            raise Stop("provider_error")

        def succeed(body, key, timeout):
            self.assertEqual(key, QWEN_KEY)
            self.assertEqual(body["model"], QWEN)
            self.assertTrue(body.get("enable_thinking") is False)
            self.assertNotIn("thinking", body)
            self.assertEqual(body["temperature"], 0)
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.calls.append(("qwen", body, key))
            return qwen_completion()

        opts = {"fallback_transport": qwen or succeed, "fallback_key_loader": lambda: QWEN_KEY}
        if deepseek is not None:
            opts["transport"] = deepseek
        else:
            opts["transport"] = fail
        opts.update(kwargs)
        return self.run_page(url, **opts)

    def test_provider_errors_fall_back_to_qwen(self):
        for code in ("provider_error", "provider_http_error"):
            with self.subTest(code=code):
                self.root = self.base / code
                self.calls.clear()

                def deepseek(*_):
                    self.calls.append("deepseek")
                    raise Stop(code)

                result = self.fallback(URL + code, deepseek=deepseek)
                self.assertEqual(result["outcome"], "ok")
                self.assertEqual(result["provider"], "qwen")
                self.assertEqual(result["returned_model"], QWEN)
                self.assertEqual(result["fallback_from"], code)
                self.assertEqual(result["provider_calls"], 2)
                self.assertEqual(result["charged_ceiling_usd"], "0.6049152")
                self.assertEqual(self.calls[0], "deepseek")
                self.assertEqual(self.calls[1][0], "qwen")
                self.assertNotIn(QWEN_KEY.encode(), self.artifacts())
                self.assertNotIn(QWEN_KEY, dumps(result))

    def test_primary_success_does_not_call_qwen(self):
        def qwen(*_):
            self.fail("qwen called on primary success")

        result = self.run_page(fallback_transport=qwen, fallback_key_loader=lambda: QWEN_KEY)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["returned_model"], "deepseek-flash")
        self.assertEqual(result["provider_calls"], 1)
        self.assertIsNone(result["fallback_from"])
        self.assertEqual(len(self.calls), 1)

    def test_injected_transport_stays_primary_only_without_fallback(self):
        def fail(*_):
            self.calls.append(1)
            raise Stop("provider_error")

        with patch.object(common, "load_qwen_key", side_effect=AssertionError("qwen key loaded")):
            result = self.run_page(transport=fail)
        self.assertEqual(result["outcome"], "provider_error")
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["provider_calls"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_validation_failure_does_not_fall_back(self):
        raw = completion()
        raw["model"] = "other-model"
        calls = []

        def qwen(*_):
            calls.append(1)
            return qwen_completion()

        result = self.run_page(transport=lambda *_: raw, fallback_transport=qwen,
                               fallback_key_loader=lambda: QWEN_KEY)
        self.assertEqual(result["outcome"], "model_mismatch")
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["provider_calls"], 1)
        self.assertEqual(calls, [])

    def test_both_providers_fail_records_and_continues_budget(self):
        def qwen(*_):
            self.calls.append("qwen")
            raise TimeoutError("lost")

        first = self.fallback(URL + "1", qwen=qwen)
        self.assertEqual(first["outcome"], "provider_error")
        self.assertEqual(first["provider"], "qwen")
        self.assertEqual(first["provider_calls"], 2)
        self.assertEqual(first["fallback_from"], "provider_error")
        # A later page can still run; the failed fallback consumed two call units.
        self.config["daily_call_cap"] = 3
        self.write_config()
        self.assertEqual(self.run_page(URL + "2")["outcome"], "ok")
        self.assertEqual(self.run_page(URL + "3")["reason"], "budget_exhausted")

    def test_fallback_counts_toward_the_same_call_cap(self):
        self.config["daily_call_cap"] = 2
        self.write_config()
        self.assertEqual(self.fallback(URL + "1")["outcome"], "ok")
        # Two units used; a further primary-only page is denied.
        self.assertEqual(self.run_page(URL + "2")["reason"], "budget_exhausted")

    def test_no_fallback_when_cap_has_only_one_unit_left(self):
        self.config["daily_call_cap"] = 1
        self.write_config()
        qcalls = []

        def qwen(*_):
            qcalls.append(1)
            return qwen_completion()

        result = self.fallback(qwen=qwen)
        self.assertEqual(result["outcome"], "provider_error")
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["provider_calls"], 1)
        self.assertIsNone(result["fallback_from"])
        self.assertEqual(qcalls, [])
        self.assertEqual(result["charged_ceiling_usd"], "0.3024576")

    def test_missing_qwen_key_skips_fallback(self):
        def qwen(*_):
            self.fail("qwen called without a key")

        result = self.run_page(transport=lambda *_: (_ for _ in ()).throw(Stop("provider_error")),
                               fallback_transport=qwen, fallback_key_loader=lambda: "")
        self.assertEqual(result["outcome"], "provider_error")
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["provider_calls"], 1)

    def test_qwen_key_never_logged_and_rejected_in_response(self):
        def qwen(*_):
            raw = qwen_completion()
            raw["note"] = QWEN_KEY
            return raw

        result = self.fallback(qwen=qwen)
        self.assertEqual(result["outcome"], "credential_in_response")
        self.assertEqual(result["provider"], "qwen")
        self.assertNotIn(QWEN_KEY, dumps(result))
        self.assertNotIn(QWEN_KEY.encode(), self.artifacts())

    def test_load_qwen_key_missing_and_permissions(self):
        missing = self.base / "absent"
        self.assertEqual(common.load_qwen_key(missing), "")
        path = self.base / "qwen.key"
        path.write_text(QWEN_KEY)
        path.chmod(0o644)
        with self.assertRaises(Stop):
            common.load_qwen_key(path)
        path.chmod(0o600)
        self.assertEqual(common.load_qwen_key(path), QWEN_KEY)
        path.write_text(QWEN_KEY + "\n")
        self.assertEqual(common.load_qwen_key(path), QWEN_KEY)
        link = self.base / "link"
        link.symlink_to(path)
        with self.assertRaises(Stop):
            common.load_qwen_key(link)

    def test_qwen_http_endpoint_is_allowlisted_direct_and_sanitized(self):
        seen = []

        class Response:
            status = 200
            headers = type("Headers", (), {"get_content_type": lambda _: "application/json"})()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, n):
                return dumps(qwen_completion()).encode()

        with patch.object(runner.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = Response()
            raw = runner.qwen_provider({"model": QWEN}, QWEN_KEY, 30)
            request = build.return_value.open.call_args.args[0]
            seen.append(request)
            self.assertEqual(raw["model"], QWEN)
            self.assertEqual(request.full_url, runner.QWEN_ENDPOINT)
            self.assertEqual(request.get_header("Authorization"), "Bearer " + QWEN_KEY)
            self.assertNotIn(QWEN_KEY.encode(), request.data)
            args = build.call_args.args
            self.assertEqual(args[0].proxies, {})
            self.assertIs(args[1], runner.NoRedirect)

        with patch.object(runner.urllib.request, "build_opener") as build:
            class Lost:
                def open(self, *a, **kw):
                    raise TimeoutError(QWEN_KEY)

            build.return_value = Lost()
            with self.assertRaisesRegex(Stop, "^provider_error$"):
                runner.qwen_provider({}, QWEN_KEY, 30)

        with patch.object(runner.urllib.request, "build_opener", side_effect=AssertionError("must not connect")):
            with self.assertRaisesRegex(Stop, "^blocked_network$"):
                runner.provider({}, QWEN_KEY, 30, endpoint="https://evil.example/chat/completions")
            with self.assertRaisesRegex(Stop, "^blocked_network$"):
                runner.provider({}, QWEN_KEY, 30, endpoint="http://token-plan.maas.qwencloudapi.com/compatible-mode/v1/chat/completions")

        self.assertTrue(runner.QWEN_ENDPOINT.startswith("https://token-plan.maas.qwencloudapi.com"))
        self.assertIn("token-plan.maas.qwencloudapi.com", runner.ALLOWED_PROVIDER_HOSTS)
        self.assertIn("api.deepseek.com", runner.ALLOWED_PROVIDER_HOSTS)


class ShippedConfigUnchanged(unittest.TestCase):
    def test_repo_default_stays_disabled(self):
        shipped = json.loads(common.CONFIG.read_text())
        self.assertIs(shipped["enabled"], False)
        self.assertEqual(shipped["model"], "deepseek-flash")
