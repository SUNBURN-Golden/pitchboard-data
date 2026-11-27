"""Codex (GPT-6.2-Sol, effort high) runs only while DeepSeek is blocked. No live calls."""
import json
import subprocess
import unittest
from unittest.mock import Mock, patch
import urllib.error

from pbdata.collect.shadow import codex, runner, schema
from pbdata.collect.shadow.common import Stop, dumps

from test_llm_extract import KEY, URL, Harness, completion, proposal


def codex_completion(fields=None, reasoning=500):
    raw = completion(fields)
    raw["model"] = codex.CODEX_MODEL
    raw["usage"] = {"prompt_tokens": 123, "completion_tokens": 100 + reasoning,
                    "total_tokens": 223 + reasoning, "completion_tokens_details": {"reasoning_tokens": reasoning}}
    return raw


class CodexFallbackTests(Harness):
    def codex(self, body, key, timeout):
        # Same request body (prompt, messages) DeepSeek would have received.
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["messages"][0]["content"], schema.PROMPT)
        self.calls.append(("codex", body))
        return codex_completion()

    def test_balance_unavailable_skips_deepseek_and_records_codex(self):
        def deepseek(*_):
            raise AssertionError("DeepSeek must not be called while unavailable")
        result = self.run_page(transport=deepseek, codex_transport=self.codex,
                               deepseek_available=lambda key: False)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["provider_model"], "gpt-6.2-sol")
        self.assertEqual(result["returned_model"], "gpt-6.2-sol")
        self.assertEqual(result["reasoning_effort"], "high")
        self.assertEqual(result["fallback_from"], "deepseek_unavailable")
        self.assertEqual(result["provider_calls"], 1)
        self.assertEqual([c["provider"] for c in result["calls"]], ["codex"])
        self.assertEqual(result["calls"][0]["model"], "gpt-6.2-sol")
        self.assertEqual(result["usage"]["reasoning_tokens"], 500)
        self.assertIsNotNone(result["fields"])
        self.assert_persisted_record(result)

    def test_http_402_falls_back_to_codex_not_qwen(self):
        def deepseek(*_):
            self.calls.append("deepseek")
            raise Stop("provider_insufficient_balance")
        qwen = Mock(side_effect=AssertionError("qwen"))
        result = self.run_page(transport=deepseek, codex_transport=self.codex, fallback_transport=qwen,
                               deepseek_available=lambda key: True)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["fallback_from"], "provider_insufficient_balance")
        self.assertEqual([c["provider"] for c in result["calls"]], ["deepseek", "codex"])
        qwen.assert_not_called()

    def test_available_deepseek_never_calls_codex(self):
        cx = Mock(side_effect=AssertionError("codex"))
        result = self.run_page(codex_transport=cx, deepseek_available=lambda key: True)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["provider"], "deepseek")
        self.assertIsNone(result["fallback_from"])
        cx.assert_not_called()

    def test_grounding_checks_still_apply_to_codex(self):
        bad = proposal()
        bad["pay"]["quote"] = "We pay USD 900 per story."
        result = self.run_page(transport=Mock(), deepseek_available=lambda key: False,
                               codex_transport=lambda *a: codex_completion(bad))
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["outcome"], "ok")
        self.assertIsNone(result["fields"]["pay"])
        self.assertIn("pay", result["validation_reasons"])
        self.assertIsNotNone(result["fields"]["deadline"])

    def test_codex_model_mismatch_is_rejected(self):
        raw = codex_completion()
        raw["model"] = "gpt-6.2-astra"
        result = self.run_page(transport=Mock(), deepseek_available=lambda key: False,
                               codex_transport=lambda *a: raw)
        self.assertEqual(result["outcome"], "model_mismatch")

    def test_insufficient_balance_detection(self):
        self.assertTrue(runner.insufficient_balance({"error": {"message": "Insufficient Balance", "type": "unknown_error"}}))
        self.assertFalse(runner.insufficient_balance({"error": {"message": "rate limited"}}))
        with patch.object(runner.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = urllib.error.HTTPError(runner.ENDPOINT, 402, "x", {}, None)
            with self.assertRaisesRegex(Stop, "^provider_insufficient_balance$"):
                runner.provider({"model": "deepseek-flash"}, KEY, 5)


class CodexProviderTests(unittest.TestCase):
    def body(self):
        return {"model": "deepseek-flash", "messages": [
            {"role": "system", "content": schema.PROMPT}, {"role": "user", "content": dumps({"as_of": "2026-11-27", "page_text": "x"})}]}

    def fake_run(self, events, answer='{"a": 1}', code=0):
        def run(cmd, **kw):
            self.cmd, self.kw = cmd, kw
            out = cmd[cmd.index("-o") + 1]
            if answer is not None:
                open(out, "w").write(answer)
            return subprocess.CompletedProcess(cmd, code, "\n".join(json.dumps(e) for e in events), "stderr secret")
        return run

    def test_command_model_effort_and_tool_lockdown(self):
        events = [{"type": "item.completed", "item": {"type": "agent_message", "text": '{"a": 1}'}},
                  {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 7, "reasoning_output_tokens": 3}}]
        raw = codex.codex_provider(self.body(), KEY, 30, run=self.fake_run(events), bin_path="/bin/true")
        self.assertIn("gpt-6.2-sol", self.cmd)
        self.assertIn('model_reasoning_effort="high"', self.cmd)
        self.assertIn("read-only", self.cmd)
        for f in ("shell_tool", "unified_exec", "browser_use"):
            self.assertIn(f, self.cmd)
        self.assertIn(schema.PROMPT, self.kw["input"])
        self.assertNotIn(KEY, " ".join(self.cmd) + self.kw["input"])
        self.assertEqual(raw["model"], "gpt-6.2-sol")
        self.assertEqual(raw["usage"], {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17,
                                        "completion_tokens_details": {"reasoning_tokens": 3},
                                        "prompt_tokens_details": {"cached_tokens": 0}})
        self.assertEqual(raw["choices"][0]["message"]["content"], '{"a": 1}')

    def test_tool_use_and_failures_fail_closed(self):
        usage = {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}
        tool = {"type": "item.completed", "item": {"type": "command_execution", "command": "cat secret"}}
        with self.assertRaisesRegex(Stop, "^invalid_completion$"):
            codex.codex_provider(self.body(), KEY, 30, run=self.fake_run([tool, usage]), bin_path="/bin/true")
        with self.assertRaisesRegex(Stop, "^provider_error$"):
            codex.codex_provider(self.body(), KEY, 30, run=self.fake_run([usage], code=1), bin_path="/bin/true")
        with self.assertRaisesRegex(Stop, "^provider_error$"):
            codex.codex_provider(self.body(), KEY, 30, run=self.fake_run([], answer=None), bin_path="/bin/true")

    def test_balance_gate_caches_and_unknown_is_available(self):
        t = [0]
        answers = iter([None, False])
        gate = codex.BalanceGate(check=lambda key: next(answers), ttl=600, clock=lambda: t[0])
        self.assertTrue(gate.available(KEY))
        t[0] = 100
        self.assertTrue(gate.available(KEY))
        t[0] = 700
        self.assertFalse(gate.available(KEY))
        gate2 = codex.BalanceGate(check=lambda key: True, clock=lambda: 0)
        gate2.mark_unavailable()
        self.assertFalse(gate2.available(KEY))


if __name__ == "__main__":
    unittest.main()
