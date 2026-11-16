"""Synthetic copies of the external schemas; eval sources remain read-only."""
from collections import Counter
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from pbdata.collect.shadow import compare, common, policy, runner, schema, state
from test_llm_extract import Harness, NOW, URL, HTML, KEY, page, proposal, completion


def fixture_eval(root):
    root.mkdir()
    def save(name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(common.dumps(value))
    def manifest(entries, directory):
        result = {}
        for url, html in entries.items():
            sha = common.digest(url)
            result[url] = {"sha": sha, "outcome": "ok", "status": 200, "ctype": "text/html", "final_url": url}
            path = root / directory / (sha + ".bin")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(html)
        return result
    root_man = manifest({URL: HTML, URL + "tune": b"<p>Tuning writing page</p>"}, "corpus")
    save("manifest.json", root_man)
    save("split.json", {URL: "heldout", URL + "tune": "tune"})
    save("d1030/manifest.json", manifest({URL + "later": HTML}, "d1030/corpus"))
    # Exact human headings/schema with all 22 mapped pages.
    b3 = {}
    for cid in compare.B3_WRITING_CID | compare.B3_NON_WRITING_CID:
        b3["https://www.arko.or.kr/board/view/4053?cid=" + cid] = HTML + ("<p>" + cid + "</p>").encode()
    for idx in compare.B3_TRANSLATION:
        b3["https://www.ltikorea.or.kr/kr/board/notice/boardView.do?bbsIdx=" + idx] = HTML + ("<p>" + idx + "</p>").encode()
    save("d1030/manifest_b3.json", manifest(b3, "d1030/corpus"))
    save("d1030/b3_gold.json", {
        "writing": ["cid=1810795 Asian Playwrights Meeting artist call",
                    "cid=1810802 문학주간 우리동네 프로그램 모집 (ambiguous)",
                    "cid=1810745 글로벌사우스 인적교류 참여자 공모 (ambiguous)",
                    "ltikorea bbsIdx x4 (translation support calls)"],
        "non_writing": [cid + " human heading" for cid in sorted(compare.B3_NON_WRITING_CID)]})
    save("d1102/manifest_ho.json", manifest({URL + "fresh": HTML + b"<p>Fresh page</p>",
                                         URL + "tune-copy": b"<p>Tuning writing page</p>"}, "d1102/corpus_ho"))
    def freeze():
        for file, names in (("split_frozen.sha256", ("split.json", "manifest.json")),
                            ("d1102/manifest_ho.sha256", ("manifest_ho.json",))):
            directory = Path(file).parent
            (root / file).write_text("".join(common.digest((root / directory / name).read_bytes()) + "  " + name + "\n" for name in names))
    freeze()
    return freeze


class CompareTests(Harness):
    def setUp(self):
        super().setUp()
        self.eval = self.base / "eval"
        self.freeze = fixture_eval(self.eval)

    def test_cohorts_dates_hashes_gold_mapping_tune_exclusion_and_dedup(self):
        items, integrity, inputs = compare.load_cohorts(self.eval)
        self.assertEqual(len(items), 25)
        self.assertEqual(len(integrity["b3_gold_mapping"]), 22)
        self.assertEqual(sum(integrity["b3_gold_mapping"].values()), 7)
        self.assertEqual(len(integrity["overlaps"]), 1)
        self.assertEqual(Counter(i.cohort for i in items), {"root": 1, "d1030": 1, "b3": 22, "d1102": 1})
        self.assertEqual(len(integrity["exclusions"]), 1)
        self.assertEqual(integrity["exclusions"][0]["reason"], "tune_overlap")
        self.assertEqual(items[0].body_hash, common.digest(HTML))
        self.assertNotEqual(items[0].body_hash, common.digest(URL))
        self.assertEqual(items[0].as_of, "2026-10-29")
        self.assertEqual(next(i for i in items if i.cohort == "d1102").as_of, "2026-11-02")
        inputs.verify()

    def test_score_missing_predictions_never_become_gold_or_negatives(self):
        items, _, _ = compare.load_cohorts(self.eval)
        report = compare.score(items, {})
        b3 = report["summaries"]["b3"]
        self.assertEqual(b3["real_call_gold"]["llm"]["labeled"], 22)
        self.assertEqual(b3["real_call_gold"]["llm"]["abstentions"], 22)
        self.assertEqual(b3["real_call_gold"]["llm"]["fn_including_abstentions"], 7)
        self.assertEqual(b3["real_call_gold"]["llm"]["recall"], 0)
        self.assertEqual(b3["pay_state"], {"paired": 0, "agreement": None})
        self.assertEqual(b3["current_open_gold"], {"accuracy": None, "labeled": 0, "reason": "missing_gold"})
        self.assertEqual(report["summaries"]["root"]["real_call_gold"]["rules"]["reason"], "missing_gold")
        self.assertEqual(report["summaries"]["deduplicated"]["eligible_pages"], 24)
        self.assertTrue(any("low-power" in note for note in report["notes"]))

    def test_hand_computed_scores_and_abstentions(self):
        result = compare.boolean_metrics([(True, True), (True, False), (True, None), (False, True), (False, False), (False, None)])
        self.assertEqual(result["tp"], 1)
        self.assertEqual(result["fp"], 1)
        self.assertEqual(result["fn_including_abstentions"], 2)
        self.assertEqual(result["tn"], 1)
        self.assertEqual(result["covered"], 4)
        self.assertEqual(result["precision"], .5)
        self.assertAlmostEqual(result["recall"], 1 / 3)
        self.assertAlmostEqual(result["f1"], .4)
        # Abstentions are wrong end-to-end (2/6) but excluded from covered (2/4).
        self.assertAlmostEqual(result["accuracy_end_to_end"], 1 / 3)
        self.assertEqual(result["accuracy_covered"], .5)
        self.assertNotIn("accuracy", result)

    def test_prediction_evidence_revalidated_and_fields_compared_as_sets(self):
        items, _, _ = compare.load_cohorts(self.eval)
        item = items[0]
        fields, _ = schema.validate(proposal(), item.page.doc(), item.as_of)
        row = {"fields": fields, "text_sha256": item.text_hash}
        report = compare.score(items, {item.key: row})
        self.assertEqual(report["summaries"]["root"]["current_open_agreement"], 1)
        self.assertEqual(report["summaries"]["root"]["deadline_agreement"], 1)
        self.assertEqual(report["summaries"]["root"]["field_coverage"]["pay"], 1)
        self.assertEqual(report["summaries"]["root"]["pay_amount"], {"paired": 1, "agreement": 1.0})
        # One LLM deadline must not match a baseline with extra dates.
        item.rules["deadline"].append("2026-12-02")
        self.assertEqual(compare.score(items, {item.key: row})["summaries"]["root"]["deadline_agreement"], 0)
        row["fields"]["pay"]["quote"] = "fabricated"
        with self.assertRaisesRegex(common.Stop, "prediction_evidence_invalid"):
            compare.score(items, {item.key: row})
        row["text_sha256"] = "wrong"
        with self.assertRaisesRegex(common.Stop, "prediction_text_mismatch"):
            compare.score(items, {item.key: row})

    def test_missing_key_record_stays_scoreable(self):
        items, _, _ = compare.load_cohorts(self.eval)
        item = items[0]
        raw = proposal()
        del raw["pay"]
        fields, reasons = schema.validate(raw, item.page.doc(), item.as_of)
        self.assertEqual(reasons, {"pay": "missing_key"})
        row = {"schema": schema.VERSION, "fields": fields, "validation_reasons": reasons,
               "text_sha256": item.text_hash}
        report = compare.score(items, {item.key: row})
        self.assertEqual(report["summaries"]["root"]["current_open_agreement"], 1)
        self.assertEqual(report["summaries"]["root"]["field_coverage"]["pay"], 0)
        self.assertEqual(report["summaries"]["root"]["field_coverage"]["deadline"], 1)

    def test_frozen_checksums_path_traversal_symlinks_and_input_changes_fail_closed(self):
        inputs = compare.Inputs(self.eval)
        inputs.read("split.json")
        (self.eval / "split.json").write_text("{}")
        with self.assertRaisesRegex(common.Stop, "eval_input_changed"):
            inputs.verify()
        with self.assertRaisesRegex(common.Stop, "eval_checksum_mismatch"):
            compare.load_cohorts(self.eval)
        with self.assertRaisesRegex(common.Stop, "unsafe_eval_path"):
            inputs.read("../config.json")
        link = self.eval / "link.json"
        link.symlink_to(self.config_path)
        with self.assertRaisesRegex(common.Stop, "unsafe_path"):
            inputs.read("link.json")
        with self.assertRaisesRegex(common.Stop, "invalid_corpus_sha"):
            compare.body(inputs, "corpus", URL, {"sha": "../../outside"})
        with self.assertRaisesRegex(common.Stop, "invalid_corpus_sha"):
            compare.body(inputs, "corpus", URL, {"sha": common.digest(HTML)})

    def test_cached_denials_and_instagram_are_exclusions_never_refetched(self):
        path = self.eval / "d1102/manifest_ho.json"
        manifest = json.loads(path.read_text())
        manifest["https://instagram.com/call"] = {"outcome": "ok", "status": 200, "ctype": "text/html"}
        manifest["https://blocked.example.org/"] = {"outcome": "denied_robots", "status": 200, "ctype": "text/html"}
        path.write_text(common.dumps(manifest))
        self.freeze()
        with patch("socket.socket", side_effect=AssertionError("network")), patch.object(policy.PolicyFetcher, "fetch", side_effect=AssertionError("refetch")):
            _, report, _ = compare.load_cohorts(self.eval)
        self.assertEqual({r["reason"] for r in report["exclusions"]}, {"blocked_host", "cached_fetch_denied", "tune_overlap"})

    def test_rules_projection_current_future_standing_closed_nonwriting_results(self):
        for html, expected in (
            (b"<h1>Writing submissions</h1><p>Submissions close December 1, 2026.</p>", True),
            (b"<h1>Writing submissions</h1><p>Submissions close March 1, 2026.</p>", False),
            (b"<h1>Writing submissions</h1><p>Submit your poems. We accept submissions year-round.</p>", True),
            (b"<h1>Poetry winners announced</h1><p>The winners of the 2026 prize.</p>", False),
            (b"<h1>Venue rental applications</h1><p>Apply for venue rental. Deadline: December 1, 2026.</p>", False),
            (b"<h1>Writing submissions</h1><p>Submit your poems.</p>", False)):
            with self.subTest(html=html):
                self.assertEqual(runner.baseline(parse_doc(html), "2026-11-05")["current_open"], expected)

    def test_score_entrypoint_offline_no_secrets_live_db_or_eval_writes(self):
        script = Path(__file__).resolve().parents[1] / "scripts/compare_llm_extract.py"
        spec = importlib.util.spec_from_file_location("compare_entry", script)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        before = {str(p.relative_to(self.eval)): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.eval.rglob("*") if p.is_file()}
        argv = [str(script), "--eval-root", str(self.eval), "--shadow-root", str(self.root)]
        with patch.object(sys, "argv", argv), patch("socket.socket", side_effect=AssertionError("network")), \
             patch.object(common, "load_key", side_effect=AssertionError("secret")), \
             patch.object(runner, "load_key", side_effect=AssertionError("secret")), \
             patch("pbdata.db.connect", side_effect=AssertionError("live DB")), \
             patch.dict(os.environ, {"DEEPSEEK_API_KEY": KEY}):
            entry.main()
        after = {str(p.relative_to(self.eval)): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.eval.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        report = json.loads((self.root / "comparison.json").read_text())
        self.assertEqual(report["summaries"]["b3"]["eligible_pages"], 22)
        self.assertNotIn(KEY.encode(), self.artifacts())

    def test_score_only_has_no_experiment_identity_and_missing_parent_is_stop(self):
        script = Path(__file__).resolve().parents[1] / "scripts/compare_llm_extract.py"
        spec = importlib.util.spec_from_file_location("compare_entry_score", script)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        argv = [str(script), "--eval-root", str(self.eval), "--shadow-root", str(self.root)]
        with patch.object(sys, "argv", argv), patch("socket.socket", side_effect=AssertionError("network")):
            entry.main()
        self.assertTrue((self.root / "comparison.json").is_file())
        for name in ("experiment.json", "experiment.lock", "budget.sqlite3"):
            self.assertFalse((self.root / name).exists(), name)
        missing = self.base / "missing-parent" / "shadow"
        result = subprocess.run([sys.executable, "-B", str(script), "--eval-root", str(self.eval),
                                 "--shadow-root", str(missing)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["reason"], "unsafe_root")
        self.assertFalse(missing.exists())

    def test_malformed_prediction_rows_raise_fixed_stop_not_keyerror(self):
        path = self.base / "bad.jsonl"
        for line in ('[]',
                     '{"outcome":"ok"}',
                     '{"outcome":"ok","requested_url":1,"body_sha256":"a","as_of":"2026-10-29"}'):
            path.write_text(line + "\n")
            with self.subTest(line=line), self.assertRaisesRegex(common.Stop, "^invalid_prediction$"):
                compare.predictions(path)
        items, _, _ = compare.load_cohorts(self.eval)
        item = items[0]
        fields, _ = schema.validate(proposal(), item.page.doc(), item.as_of)
        row = {"fields": dict(fields), "text_sha256": item.text_hash}
        row["fields"]["pay"] = []
        with self.assertRaisesRegex(common.Stop, "^invalid_prediction_fields$"):
            compare.score(items, {item.key: row})
        with self.assertRaisesRegex(common.Stop, "^invalid_prediction$"):
            compare.score(items, {item.key: "not-a-row"})

    def test_generate_loop_treats_experiment_states_as_terminal(self):
        script = Path(__file__).resolve().parents[1] / "scripts/compare_llm_extract.py"
        spec = importlib.util.spec_from_file_location("compare_entry_terminal", script)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        for reason in ("experiment_halted", "disabled", "expired_pricing", "budget_exhausted",
                       "unrecognized_shadow_root", "config_changed", "clock_backwards"):
            with self.subTest(reason=reason):
                self.assertTrue(entry.terminal({"outcome": "skipped", "reason": reason}))
        for reason in ("blocked_host", "access_wall", "page_too_large", "duplicate_page",
                       "denied_robots", "fetch_error", "invalid_policy_envelope"):
            with self.subTest(reason=reason):
                self.assertFalse(entry.terminal({"outcome": "skipped", "reason": reason}))
        self.assertTrue(entry.terminal({"outcome": "model_mismatch"}))
        self.assertTrue(entry.terminal({"outcome": "usage_invariant"}))
        self.assertFalse(entry.terminal({"outcome": "ok"}))
        self.assertFalse(entry.terminal({"outcome": "provider_error"}))
        self.assertTrue(entry.terminal(None))

    def test_cohort_threshold_shares_config_and_rejects_invalid(self):
        self.assertEqual(common.page_limit(self.config_path), 24000)
        self.config["max_page_utf8_bytes"] = 100
        self.write_config()
        self.assertEqual(common.page_limit(self.config_path), 100)
        items, integrity, _ = compare.load_cohorts(self.eval, common.page_limit(self.config_path))
        self.assertEqual(items, [])
        self.assertTrue(integrity["exclusions"])
        reasons = {e["reason"] for e in integrity["exclusions"]}
        self.assertIn("page_too_large", reasons)
        self.assertLessEqual(reasons, {"page_too_large", "tune_overlap"})
        for bad in (0, 24001, True, "24000"):
            with self.subTest(bad=bad), self.assertRaisesRegex(common.Stop, "^invalid_config$"):
                compare.load_cohorts(self.eval, bad)

    def test_generate_disabled_exits_before_eval_or_key_reads(self):
        script = Path(__file__).resolve().parents[1] / "scripts/compare_llm_extract.py"
        result = subprocess.run([sys.executable, "-B", str(script), "--operation", "generate", "--eval-root", str(self.base / "missing"),
                                 "--shadow-root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["reason"], "disabled")
        self.assertFalse(self.root.exists())

    def test_independent_processes_share_budget(self):
        self.config["daily_call_cap"] = 3
        self.write_config()
        with state.State(self.root).locked():
            pass
        program = '''
import sys
from pbdata.collect.shadow.common import Stop, load_config, stamp
from pbdata.collect.shadow.state import State
s = State(sys.argv[1])
try:
    with s.locked() as conn:
        s.reserve(conn, load_config(sys.argv[2], stamp('2026-11-05T12:00:00Z')), sys.argv[3], stamp('2026-11-05T12:00:00Z'))
    print('reserved')
except Stop:
    print('stopped')
'''
        processes = [subprocess.Popen([sys.executable, "-B", "-c", program, str(self.root), str(self.config_path), str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(10)]
        results = [p.communicate(timeout=20) for p in processes]
        self.assertEqual(sum(out.strip() == "reserved" for out, err in results), 3)
        self.assertTrue(all(not err for _, err in results))


def parse_doc(html):
    return page(body=html).doc()
