"""Synthetic shadow improvement checks; no live inputs or provider calls."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pbdata.collect.shadow import batch, common, planner, runner, schema, state
from test_llm_extract import Harness, NOW, URL, KEY, HTML, page, proposal, completion

def candidate(**changes):
    row = dict(gap_id="gap_1", subject_type="outlet", subject_id="out_1", predicate="fee.submission",
               scope_ref="unspecified", state="open", gap_class="not_extracted", importance=.7,
               owner_outlet_id="out_1", source_id="src_1", owner_name="Example", outlet_name="Example",
               permission="allowed_public", raw_id="raw_1", url=URL, final_url=URL, kind="html", outcome="ok",
               http_status=200, content_hash=common.source_content_hash(page().body), text_hash=None,
               fetched_at="2026-11-05T00:00:00Z", relevance=0, relationship_basis="outlet_source")
    row.update(changes)
    if "url" in changes and "final_url" not in changes:
        row["final_url"] = changes["url"]
    return row


def export(*rows):
    return dict(schema=planner.EXPORT_SCHEMA, exported_at_utc=NOW.isoformat(), rows=list(rows))


class PlannerTests(unittest.TestCase):
    def test_plan_priority_dedup_exclusions_latest(self):
        rows = [candidate(), candidate(gap_id="gap_2", predicate="compensation.rate"),
                candidate(gap_id="gap_3", url="https://example.net/deadline", predicate="deadline.closes_on"),
                candidate(gap_id="gap_4", predicate="policy.ai"),
                candidate(gap_id="gap_5", owner_name="SOULBOUND", url="https://other.org/"),
                candidate(gap_id="gap_6", url="https://instagram.com/a"),
                candidate(gap_id="gap_7", owner_outlet_id=None),
                candidate(gap_id="gap_8", url="https://denied.org/", raw_id="raw_z"),
                candidate(gap_id="gap_8", url="https://denied.org/", raw_id="raw_a", fetched_at="2026-11-05T01:00:00Z", outcome="denied_robots")]
        with patch("socket.socket", side_effect=AssertionError("network")):
            plan = planner.plan(export(*rows))
        self.assertEqual([t["url"] for t in plan["targets"]], [URL, "https://example.net/deadline"])
        self.assertEqual(len(plan["targets"][0]["targets"]), 2)
        self.assertEqual(set(plan["reasons"]), {"unsupported_predicate", "excluded_owner", "blocked_host", "no_outlet_relation", "superseded_observation", "ineligible_source"})
        many = export(*(candidate(gap_id=f"gap_{i}", url=f"https://example.org/{i}") for i in range(60)))
        self.assertEqual(len(planner.plan(many)["targets"]), 60)
        self.assertEqual(len(planner.plan(many, limit=51)["targets"]), 51)
        self.assertEqual(planner.plan(many, limit=0)["targets"], [])
        with self.assertRaisesRegex(common.Stop, "invalid_limit"):
            planner.plan(many, limit=-1)

    def test_planner_best_page_attempt_order_and_metadata_only(self):
        rows = [candidate(url="https://example.org/other", relevance=2), candidate(),
                candidate(gap_id="gap_2", owner_outlet_id="out_2", url="https://example.net/new")]
        p = planner.plan(export(*rows))
        self.assertEqual({r["url"] for r in p["targets"]}, {URL, "https://example.net/new"})
        history = [dict(final_url=URL, body_sha256=candidate()["content_hash"], outcome="provider_error", timestamp_utc=NOW.isoformat())]
        p = planner.plan(export(candidate(), candidate(gap_id="gap_2", url="https://example.org/new")), history=history)
        self.assertEqual(p["targets"][0]["url"], "https://example.org/new")
        with self.assertRaisesRegex(common.Stop, "invalid_gap_export"):
            planner.plan(export(candidate(body="must never enter metadata")))

    def test_shadow_excludes_soulbound_before_dns_and_legacy_score_versions(self):
        from pbdata.collect.shadow.policy import canonical_url
        with patch("socket.socket", side_effect=AssertionError("network")):
            for url in ("https://soulbound.co.kr/", "https://pitchboard.soulbound.co.kr/", "https://soulbound.kr/"):
                with self.assertRaisesRegex(common.Stop, "blocked_host"):
                    canonical_url(url)
        self.assertEqual(planner.plan(export(candidate(owner_name="소울바운드")))["reasons"], {"excluded_owner": 1})


class InputSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stale_and_static_history(self):
        p = self.tmp_path / "gaps.json"
        p.write_text(common.dumps(export(candidate())))
        with self.assertRaisesRegex(common.Stop, "stale_gap_export"):
            planner.read_json(p, NOW + dt.timedelta(days=2))
        self.assertTrue(planner.read_json(p, NOW + dt.timedelta(days=2), allow_stale=True))
        h = dict(outcome="ok", final_url=URL, body_sha256=candidate()["content_hash"],
                 prompt_sha256=schema.PROMPT_HASH, schema_sha256=schema.SCHEMA_HASH, config_sha256="config", baseline={"deadline": ["2026-11-06"]})
        self.assertFalse(planner.plan(export(candidate()), history=[h], config_hash="config")["targets"])
        self.assertTrue(planner.plan(export(candidate(predicate="status.declared")), history=[h], config_hash="config")["targets"])

    def test_db_snapshot_parity_and_no_writes(self):
        p = self.tmp_path / "fixture.sqlite"
        c = sqlite3.connect(p)
        r = candidate()
        c.executescript('''CREATE TABLE pb_gap(gap_id,subject_type,subject_id,predicate,scope_ref,state,gap_class,importance);
        CREATE TABLE pb_call_edition(call_id,outlet_id);
        CREATE TABLE pb_source(source_id,owner_outlet_id,owner_name,permission);
        CREATE TABLE pb_outlet(outlet_id,canonical_name);
        CREATE TABLE pb_raw_ref(raw_id,source_id,url,final_url,kind,outcome,http_status,content_hash,text_hash,fetched_at);
        CREATE TABLE pb_extraction(raw_id,field,value_json);
        CREATE TABLE pb_label(raw_id,label,status);''')
        for table, keys in [("pb_gap", "gap_id subject_type subject_id predicate scope_ref state gap_class importance"),
                            ("pb_source", "source_id owner_outlet_id owner_name permission"),
                            ("pb_raw_ref", "raw_id source_id url final_url kind outcome http_status content_hash text_hash fetched_at")]:
            keys = keys.split()
            c.execute(f"INSERT INTO {table} VALUES({','.join('?' for _ in keys)})", [r[k] for k in keys])
        c.execute("INSERT INTO pb_outlet VALUES('out_1','Example')")
        c.execute("INSERT INTO pb_extraction VALUES('raw_1','signal.cue',?)", ('{"for":"fee.submission"}',))
        c.commit(); c.close()
        before = (p.read_bytes(), p.stat().st_mtime_ns, set(self.tmp_path.iterdir()))
        self.assertEqual(planner.plan(planner.read_db(p, NOW)), planner.plan(export(r)))
        self.assertEqual(before, (p.read_bytes(), p.stat().st_mtime_ns, set(self.tmp_path.iterdir())))
        c = sqlite3.connect(p)
        c.execute("INSERT INTO pb_call_edition VALUES('call_1','out_1')")
        c.execute("INSERT INTO pb_call_edition VALUES('call_organizer_only',NULL)")
        c.execute("INSERT INTO pb_gap SELECT 'gap_call','call','call_1',predicate,scope_ref,state,gap_class,importance FROM pb_gap LIMIT 1")
        c.execute("INSERT INTO pb_gap SELECT 'gap_organizer','call','call_organizer_only',predicate,scope_ref,state,gap_class,importance FROM pb_gap LIMIT 1")
        c.commit(); c.close()
        joined = planner.plan(planner.read_db(p, NOW))
        self.assertEqual({t["gap_id"] for t in joined["targets"][0]["targets"]}, {"gap_1", "gap_call"})
        self.assertEqual(joined["reasons"]["no_outlet_relation"], 1)
        c = sqlite3.connect(p)
        # New denial inserted before an older success: rowid ordering would be wrong.
        c.execute("INSERT INTO pb_raw_ref SELECT 'raw_denied',source_id,url,final_url,kind,'denied_robots',403,content_hash,text_hash,'2026-11-06T00:00:00Z' FROM pb_raw_ref LIMIT 1")
        c.execute("INSERT INTO pb_raw_ref SELECT 'raw_old',source_id,url,final_url,kind,'ok',200,content_hash,text_hash,'2026-11-04T00:00:00Z' FROM pb_raw_ref LIMIT 1")
        c.commit(); c.close()
        self.assertFalse(planner.plan(planner.read_db(p, NOW))["targets"])

    def test_db_sidecars_require_export(self):
        p = self.tmp_path / "fixture.sqlite"
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE pb_gap(gap_id)")
        c.commit(); c.close()
        Path(str(p) + "-wal").touch()
        with self.assertRaisesRegex(common.Stop, "db_requires_export"):
            planner.read_db(p, NOW)

    def _fixture_db(self, path, *, wal=False):
        c = sqlite3.connect(path)
        r = candidate()
        c.executescript("""CREATE TABLE pb_gap(gap_id,subject_type,subject_id,predicate,scope_ref,state,gap_class,importance);
        CREATE TABLE pb_call_edition(call_id,outlet_id);
        CREATE TABLE pb_source(source_id,owner_outlet_id,owner_name,permission);
        CREATE TABLE pb_outlet(outlet_id,canonical_name);
        CREATE TABLE pb_raw_ref(raw_id,source_id,url,final_url,kind,outcome,http_status,content_hash,text_hash,fetched_at);
        CREATE TABLE pb_extraction(raw_id,field,value_json);
        CREATE TABLE pb_label(raw_id,label,status);""")
        for table, keys in [("pb_gap", "gap_id subject_type subject_id predicate scope_ref state gap_class importance"),
                            ("pb_source", "source_id owner_outlet_id owner_name permission"),
                            ("pb_raw_ref", "raw_id source_id url final_url kind outcome http_status content_hash text_hash fetched_at")]:
            keys = keys.split()
            c.execute(f"INSERT INTO {table} VALUES({','.join('?' for _ in keys)})", [r[k] for k in keys])
        c.execute("INSERT INTO pb_outlet VALUES('out_1','Example')")
        c.execute("INSERT INTO pb_extraction VALUES('raw_1','signal.cue',?)", ('{"for":"fee.submission"}',))
        c.commit()
        if wal:
            # journal_mode cannot change mid-transaction; WAL files appear after a later write.
            self.assertEqual(c.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            c.execute("INSERT INTO pb_call_edition VALUES('call_setup','out_1')")
            c.commit()
        return c, r

    def test_write_gap_export_from_wal_does_not_write_source(self):
        db = self.tmp_path / "fixture.sqlite"
        tmp = self.tmp_path / "tmp"
        tmp.mkdir()
        out = self.tmp_path / "gaps.json"
        conn, row = self._fixture_db(db, wal=True)
        sources = [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")]
        self.assertTrue((self.tmp_path / "fixture.sqlite-wal").exists())
        def snapshot():
            return tuple((path.name, path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
                         for path in sources if path.exists())
        before = snapshot()
        data = planner.write_gap_export(db, out, NOW, tmp_dir=tmp)
        self.assertEqual(before, snapshot())
        self.assertEqual(data["schema"], planner.EXPORT_SCHEMA)
        self.assertEqual(planner.plan(data), planner.plan(export(row)))
        self.assertEqual(planner.read_json(out, NOW)["rows"], data["rows"])
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(tmp.glob("pb-shadow-gaps-*")))
        conn.close()

    def test_write_gap_export_cli_and_daily_wrapper(self):
        db = self.tmp_path / "fixture.sqlite"
        conn, row = self._fixture_db(db)
        conn.close()
        out = self.tmp_path / "gaps.json"
        script = Path(__file__).resolve().parents[1] / "scripts/llm_extract_shadow_export.py"
        result = subprocess.run([sys.executable, "-B", str(script), "--service-db", str(db),
                                 "--output", str(out), "--tmp-dir", str(self.tmp_path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["outcome"], "ok")
        self.assertEqual(payload["rows"], 1)
        got = planner.read_json(out, common.utcnow())
        self.assertEqual([item["gap_id"] for item in got["rows"]], [row["gap_id"]])
        self.assertEqual(planner.plan({**got, "exported_at_utc": NOW.isoformat()})["targets"],
                         planner.plan(export(row))["targets"])
        daily = Path(__file__).resolve().parents[1] / "scripts/llm_extract_shadow_daily.sh"
        syntax = subprocess.run(["bash", "-n", str(daily)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        text = daily.read_text()
        self.assertIn("scripts/llm_extract_shadow_export.py", text)
        self.assertIn("scripts/llm_extract_shadow_batch.py", text)
        self.assertIn("pbdata.collect.egress wait", text)
        self.assertIn("PB_SHADOW_LIMIT:-40", text)
        self.assertIn("chmod 0700", text)
        self.assertNotIn("daily.lock", text)
        self.assertNotIn("collector.lock", text)

    def test_cli_disabled_and_plan_only(self):
        script = Path(__file__).resolve().parents[1] / "scripts/llm_extract_shadow_batch.py"
        p = self.tmp_path / "gaps.json"
        p.write_text(common.dumps(export(candidate())))
        root = self.tmp_path / "shadow"
        args = [sys.executable, "-B", str(script), "--gaps-json", str(p), "--shadow-root", str(root)]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "disabled")
        self.assertFalse(root.exists())
        result = subprocess.run(args + ["--plan-only", "--allow-stale"], capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["selected"], 1)
        self.assertFalse(root.exists())

    def test_gap_inputs_require_regular_files(self):
        fifo = self.tmp_path / "gaps.json"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(common.Stop, "unsafe_file"):
            planner.read_json(fifo, NOW)
        with self.assertRaisesRegex(common.Stop, "unsafe_file"):
            planner.read_db(fifo, NOW)
        with self.assertRaisesRegex(common.Stop, "unsafe_file"):
            planner.read_json(self.tmp_path, NOW)


class BatchUnitTests(unittest.TestCase):
    def test_unknown_results_stop(self):
        for r in ({"outcome": "surprise"}, {"outcome": "ok", "reason": "experiment_halted"},
                  {"outcome": "credential_in_response"}, None):
            self.assertTrue(batch.terminal(r))
        self.assertFalse(batch.terminal({"outcome": "provider_error"}))


class CacheTests(Harness):
    def test_matching_copy_no_fetch_baseline_roundtrip_and_corruption(self):
        from pbdata.collect.shadow import source_cache
        first = self.run_page()
        self.assertEqual(first["outcome"], "ok")
        cached = source_cache.load(self.root, URL, page().body_sha256, NOW)
        self.assertEqual(cached.document, page().doc())
        self.assertEqual(runner.baseline(cached.doc(), "2026-11-05"), first["baseline"])
        self.assertEqual(cached.provenance, "shadow_cached")
        cached.provenance = "live_verified"
        with self.assertRaisesRegex(common.Stop, "invalid_policy_envelope"):
            cached.doc()
        target = planner.plan(export(candidate()))["targets"][0]
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=AssertionError("refetch")), patch("socket.socket", side_effect=AssertionError("network")):
            r = runner.ShadowRunner(self.config_path, clock=lambda: NOW).run(shadow_root=self.root, url=URL, target=target,
                    transport=self.transport, key_loader=lambda: KEY)
        self.assertEqual(r["outcome"], "ok")
        self.assertEqual(r["provenance"], "shadow_cached")
        path = self.root / "data/shadow/source_text" / (first["source_cache_key"] + ".json")
        raw = json.loads(path.read_text()); raw["payload"]["doc"]["title"] = "changed"
        path.write_text(common.dumps(raw))
        with self.assertRaisesRegex(common.Stop, "corrupt_source_cache"):
            source_cache.load(self.root, URL, page().body_sha256, NOW)

    def test_missing_null_oversize_expired_and_save_failure_no_spend(self):
        from pbdata.collect.shadow import source_cache
        self.assertIsNone(source_cache.load(self.root, URL, None, NOW))
        with patch.object(source_cache, "save", side_effect=common.Stop("unsafe_path")):
            self.assertEqual(self.run_page()["reason"], "unsafe_path")
        with state.State(self.root).locked() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)
        self.run_page()
        with self.assertRaisesRegex(common.Stop, "page_too_large"):
            source_cache.load(self.root, URL, page().body_sha256, NOW, limit=10)
        self.assertIsNone(source_cache.load(self.root, URL, page().body_sha256, NOW + dt.timedelta(days=30)))
        self.assertNotIn(KEY.encode(), self.artifacts())

    def test_cache_preserves_table_metadata_and_utf8(self):
        from pbdata.collect.shadow import source_cache
        p = page(body='<html lang="ko"><title>모집</title><meta property="og:title" content="시 모집"><table><tr><td>응모료</td><td>10,000원</td></tr></table></html>'.encode())
        r = self.run_page(page=p)
        self.assertEqual(r["outcome"], "ok")
        cached = source_cache.load(self.root, URL, p.source_content_hash, NOW)
        self.assertEqual(cached.doc().segments, p.doc().segments)
        self.assertEqual(cached.doc().meta, p.doc().meta)
        self.assertEqual(runner.baseline(cached.doc(), r["as_of"]), r["baseline"])


def validate_one(field, value, quote, text=None):
    from pbdata.collect.parse import Doc, Segment
    raw = dict.fromkeys(schema.FIELDS)
    raw[field] = dict(value=value, quote=quote)
    return schema.validate(raw, Doc("html", segments=[Segment(0, "p", text or quote)]), "2026-11-05")


def fee(text="Entry fee USD 20", **changes):
    value = dict(text=text, state="stated", amount_min="20", amount_max="20", currency="USD", unit="per_submission")
    value.update(changes)
    return value


class ValidationTests(unittest.TestCase):
    def test_strict_tokens_fee_date_currency_unit_and_negation(self):
        q = "Entry fee USD 20"
        self.assertTrue(validate_one("submission_fee", fee(), q)[0]["submission_fee"])
        for value in (fee(amount_min="2"), fee(currency="KRW"), fee(unit="per_word"), fee(text="invented")):
            self.assertIsNone(validate_one("submission_fee", value, q)[0]["submission_fee"])
        for q in ("No entry fee is stated.", "There is a prize USD 20."):
            self.assertIsNone(validate_one("submission_fee", fee(text=q), q)[0]["submission_fee"])
        q = "No submission fee."
        self.assertTrue(validate_one("submission_fee", fee(text=q, state="explicitly_none", amount_min=None, amount_max=None, currency=None, unit=None), q)[0]["submission_fee"])
        for q, amount in (("응모료 10,000원", "10000"), ("응모료 ２만원", "20000"), ("Entry fee USD 1,000.50", "1000.50")):
            v = fee(text=q, amount_min=amount, amount_max=amount, currency="KRW" if "원" in q else "USD")
            self.assertTrue(validate_one("submission_fee", v, q)[0]["submission_fee"])
        q = "Entry fee USD 2026"
        self.assertEqual(validate_one("submission_fee", fee(text=q), q)[1]["submission_fee"], "unsupported_amount")
        q = "Submissions close December 1, 2026."
        v = dict(text="December 1, 2026", date="2026-12-02", timezone=None)
        self.assertEqual(validate_one("deadline", v, q)[1]["deadline"], "unsupported_date")
        v.update(date="2026-12-01", timezone="KST")
        self.assertEqual(validate_one("deadline", v, q)[1]["deadline"], "unsupported_timezone")
        v = dict(is_real_call=True, status="open")
        self.assertIsNone(validate_one("open_call", v, "We are not currently open to submissions.")[0]["open_call"])
        self.assertIsNone(validate_one("open_call", dict(is_real_call=False, status="not_call"), "Submissions are open.")[0]["open_call"])
        raw = proposal(); raw.pop("submission_fee"); raw.pop("simultaneous")
        self.assertTrue(schema.validator("pb-shadow-1")(raw, page().doc(), "2026-11-05")[0]["pay"])

    def test_ambiguous_currency_does_not_infer_usd(self):
        q = "Entry fee $20"
        self.assertEqual(validate_one("submission_fee", fee(text=q), q)[1]["submission_fee"], "unsupported_currency")

    def test_omitting_deadline_cannot_keep_past_open_call(self):
        quote = "Submissions are open until December 1, 2025."
        fields, reasons = validate_one("open_call", dict(is_real_call=True, status="open"), quote)
        self.assertIsNone(fields["open_call"])
        self.assertEqual(reasons["open_call"], "past_deadline_conflict")

    def test_date_components_cannot_support_fee_amounts(self):
        quote = "Entry fee. Submissions close December 2026."
        value = fee(text=quote, amount_min="2026", amount_max="2026", currency=None, unit=None)
        fields, reasons = validate_one("submission_fee", value, quote)
        self.assertIsNone(fields["submission_fee"])
        self.assertEqual(reasons["submission_fee"], "unsupported_amount")
        quote = "Entry fee USD 20. Submissions close December 2026."
        self.assertTrue(validate_one("submission_fee", fee(text="Entry fee USD 20"), quote)[0]["submission_fee"])


class ReviewTests(Harness):
    def test_conflicts_flags_missing_baseline_and_recover_once(self):
        from pbdata.collect.shadow.review import reviews
        row = self.run_page()
        changed = copy.deepcopy(row)
        changed["fields"]["deadline"]["value"]["date"] = "2026-12-02"
        self.assertTrue(any("field_conflict" in r["reasons"] for r in reviews(changed)))
        changed = copy.deepcopy(row)
        changed["fields"]["open_call"]["value"] = dict(is_real_call=False, status="not_call")
        self.assertTrue(any(r["second_opinion_required"] for r in reviews(changed)))
        changed = copy.deepcopy(row)
        changed["baseline"].update(current_open=False, real_call=False, labels=[dict(status="results_not_call")])
        self.assertTrue(any(r["second_opinion_required"] for r in reviews(changed)))
        changed["baseline"].update(labels=[], extensions=[], deadline=[])
        rs = reviews(changed)
        self.assertFalse(any(r["second_opinion_required"] for r in rs))
        self.assertTrue(any("baseline_uncertain" in r["reasons"] for r in rs))
        # Missing baseline fields produce candidate fills, never conflicts.
        self.assertTrue(any(r["reasons"] == ["candidate_fill"] for r in rs))
        # A failure publishing derived rows happens after the completed attempt
        # is durable; recovery must publish them without another provider call.
        self.root = self.base / "recover"
        with patch.object(state.State, "publish_derived", side_effect=OSError("failure")), patch.object(runner, "baseline", return_value=changed["baseline"]):
            self.assertEqual(self.run_page()["outcome"], "skipped")
        with state.State(self.root).locked():
            pass
        paths = list((self.root / "data/shadow").glob("review_conflicts_*.jsonl"))
        self.assertTrue(paths)
        before = [p.read_bytes() for p in paths]
        with state.State(self.root).locked():
            pass
        self.assertEqual(before, [p.read_bytes() for p in paths])
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn(KEY.encode(), self.artifacts())


class DigestTests(Harness):
    def test_default_off_ko_en_zero_calls_and_recovery(self):
        row = self.run_page()
        self.assertEqual(row["digest_drafts"], [])
        self.assertFalse(list(self.root.glob("data/shadow/digest_drafts_*.jsonl")))
        self.root = self.base / "drafts"
        # Interrupt publication after result durability.
        with patch.object(state.State, "publish_derived", side_effect=OSError("failure")):
            self.assertEqual(self.run_page(digest_languages=("ko", "en"))["outcome"], "skipped")
        with state.State(self.root).locked():
            pass
        p = next(self.root.glob("data/shadow/digest_drafts_*.jsonl"))
        rows = [json.loads(line) for line in p.read_text().splitlines()]
        self.assertEqual({r["language"] for r in rows}, {"ko", "en"})
        self.assertTrue(all(len(r["line"]) <= 280 and "\n" not in r["line"] and r["draft"] for r in rows))
        before = p.read_bytes()
        with state.State(self.root).locked():
            pass
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual(len(self.calls), 2)
        from pbdata.collect.shadow.digest import drafts
        row["reviews"] = [dict(second_opinion_required=True, reasons=["call_disagreement"])]
        self.assertFalse(drafts(row, ("ko",)))
        row["reviews"] = []; row["fields"] = dict.fromkeys(schema.FIELDS)
        self.assertFalse(drafts(row, ("en",)))
        self.assertNotIn(KEY.encode(), self.artifacts())


class ReportTests(Harness):
    def inputs(self):
        from pbdata.collect.shadow.report import score_shadow
        row = self.run_page(target=planner.plan(export(candidate()))["targets"][0])
        self.assertEqual(row["outcome"], "ok")
        return row, next(self.root.glob("data/shadow/llm_extract_*.jsonl")), score_shadow

    def test_missing_key_record_stays_scoreable(self):
        from pbdata.collect.shadow.report import score_shadow
        raw = proposal()
        del raw["pay"]
        row = self.run_page(transport=lambda *_: completion(raw))
        self.assertEqual(row["validation_reasons"], {"pay": "missing_key"})
        path = next(self.root.glob("data/shadow/llm_extract_*.jsonl"))
        report = score_shadow([path], self.root, NOW, self.config, ledger_path=self.root / "budget.sqlite3")
        self.assertEqual(report["counts"]["verified_unique_pages"], 1)
        self.assertEqual(report["exclusions"], [])
        self.assertEqual(report["field_coverage"]["pay"]["accepted"], 0)
        self.assertEqual(report["field_coverage"]["deadline"]["accepted"], 1)

    def test_empty_object_record_zero_coverage_and_planner_done_parity(self):
        from pbdata.collect.shadow.report import score_shadow
        row = self.run_page(transport=lambda *_: completion({}))
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual(row["fields"], dict.fromkeys(schema.FIELDS))
        self.assertEqual(row["validation_reasons"], dict.fromkeys(schema.FIELDS, "missing_key"))
        path = next(self.root.glob("data/shadow/llm_extract_*.jsonl"))
        report = score_shadow([path], self.root, NOW, self.config, ledger_path=self.root / "budget.sqlite3")
        self.assertEqual(report["counts"]["verified_unique_pages"], 1)
        self.assertEqual(report["exclusions"], [])
        for field in schema.FIELDS:
            self.assertEqual(report["field_coverage"][field]["accepted"], 0)
        # The done gate keys on outcome+hashes, not claims: a {} completion
        # suppresses static retries exactly like an explicit all-null completion.
        empty_plan = planner.plan(export(candidate()), history=state.read_history(self.root))
        self.assertFalse(empty_plan["targets"])
        self.assertEqual(empty_plan["reasons"]["skip_static_already_done"], 1)
        self.root = self.base / "all_null"
        nulls = self.run_page(transport=lambda *_: completion(dict.fromkeys(schema.FIELDS)))
        self.assertEqual(nulls["outcome"], "ok")
        self.assertEqual(nulls["validation_reasons"], {})
        self.assertEqual(empty_plan, planner.plan(export(candidate()), history=state.read_history(self.root)))

    def test_offline_missing_gold_fee_denominators_cost_and_unfinished(self):
        row, path, score_shadow = self.inputs()
        self.run_page(url="https://example.org/failed", transport=lambda *_: (_ for _ in ()).throw(common.Stop("provider_error")))
        with state.State(self.root).locked() as c:
            ledger = state.State(self.root)
            attempt, _ = ledger.reserve(c, "unfinished", NOW)
            ledger.record_call(c, self.config, NOW, attempt, "deepseek")
        with patch("socket.socket", side_effect=AssertionError("network")), patch.object(runner, "provider", side_effect=AssertionError("provider")), patch.object(common, "load_key", side_effect=AssertionError("key")):
            report = score_shadow([path], self.root, NOW, self.config, ledger_path=self.root / "budget.sqlite3")
        self.assertEqual(report["counts"]["attempted"], 2)
        self.assertEqual(report["counts"]["verified_unique_pages"], 1)
        self.assertEqual(report["field_coverage"]["submission_fee"]["targeted_pages"], 1)
        self.assertEqual(report["field_coverage"]["submission_fee"]["accepted"], 0)
        self.assertIsNone(report["accuracy"]["real_call"]["llm"]["accuracy_end_to_end"])
        self.assertEqual(report["accuracy"]["real_call"]["llm"]["reason"], "missing_gold")
        self.assertEqual(report["ledger"]["ceiling_usd"], "0.9073728")
        day = report["ledger"]["days"]["2026-11-05"]
        self.assertEqual((day["attempts"], day["failed"], day["unfinished"], day["unknown_usage"]), (3, 1, 1, 2))
        self.assertEqual(day["provider_calls"], 3)
        self.assertIsNone(day["estimated_cost_usd"])
        self.assertTrue(report["runtime_commits"])

    def test_gold_accuracy_fields_invalid_evidence_missing_text_duplicates(self):
        row, path, score_shadow = self.inputs()
        names = ("requested_url", "final_url", "body_sha256", "text_sha256", "as_of", "schema_sha256", "prompt_sha256")
        label = {k: row[k] for k in names}
        label.update(scope_ref="page", reviewer="fixture reviewer", reviewed_at_utc=NOW.isoformat(),
                     real_call=True, current_open=False, fields={"pay": row["fields"]["pay"]["value"], "submission_fee": None},
                     evidence={"real_call": row["fields"]["open_call"]["quote"], "current_open": row["fields"]["open_call"]["quote"], "pay": row["fields"]["pay"]["quote"]})
        gold = self.base / "gold.json"
        gold.write_text(common.dumps(dict(schema="pb-shadow-gold-1", rows=[label])))
        report = score_shadow([path], self.root, NOW, self.config, gold_path=gold)
        self.assertEqual(report["accuracy"]["real_call"]["llm"]["tp"], 1)
        self.assertEqual(report["accuracy"]["current_open"]["llm"]["fp"], 1)
        self.assertEqual(report["field_accuracy"]["pay"]["llm"]["tp"], 1)
        self.assertEqual(report["field_accuracy"]["pay"]["rules"]["tp"], 1)
        self.assertEqual(report["ledger"]["reason"], "missing_ledger")
        duplicate = self.base / "duplicate.jsonl"
        changed = copy.deepcopy(row); changed["fields"]["pay"]["quote"] = "invented"
        duplicate.write_text(common.dumps(row) + "\n" + common.dumps(changed) + "\n")
        with self.assertRaisesRegex(common.Stop, "conflicting_predictions"):
            score_shadow([duplicate], self.root, NOW, self.config)
        duplicate.write_text(common.dumps(changed) + "\n")
        report = score_shadow([duplicate], self.root, NOW, self.config)
        self.assertEqual(report["exclusions"][0]["reason"], "invalid_evidence")
        cache = self.root / "data/shadow/source_text" / (row["source_cache_key"] + ".json")
        cache.unlink()
        report = score_shadow([path], self.root, NOW, self.config)
        self.assertEqual(report["counts"]["verified_unique_pages"], 0)
        self.assertEqual(report["exclusions"][0]["reason"], "unverifiable_missing_or_expired_text")
        self.assertIsNone(report["field_coverage"]["submission_fee"]["coverage"])
        duplicate.write_text("{}")
        with self.assertRaisesRegex(common.Stop, "partial_shadow_output"):
            score_shadow([duplicate], self.root, NOW, self.config)

    def test_offline_cli_subprocess_tripwires(self):
        row, path, _ = self.inputs()
        script = Path(__file__).resolve().parents[1] / "scripts/compare_llm_extract.py"
        program = '''
import runpy, sys
from unittest.mock import patch
import pbdata.collect.shadow.common as common
import pbdata.collect.shadow.runner as runner
with patch('socket.socket', side_effect=AssertionError('network')), patch.object(common, 'load_key', side_effect=AssertionError('secret')), patch.object(runner, 'provider', side_effect=AssertionError('provider')), patch('pbdata.db.connect', side_effect=AssertionError('live DB')):
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name='__main__')
'''
        args = [sys.executable, "-B", "-c", program, str(script), "--input-mode", "shadow", "--shadow-root", str(self.root),
                "--predictions", str(path), "--config", str(self.config_path), "--as-of", "2026-11-05"]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["verified_unique_pages"], 1)
        report_path = self.root / "comparison_20261105.json"
        self.assertTrue(report_path.exists() and report_path.with_suffix(".md").exists())
        self.assertNotIn(KEY, report_path.read_text())


class BatchIntegrationTests(Harness):
    def test_batch_selection_override_and_private_plan(self):
        plan = planner.plan(export(*(candidate(gap_id=f"gap_{i}", url=f"https://example.org/{i}") for i in range(4))),
                            limit=2)
        fetched = []
        def fetch(url):
            fetched.append(url)
            return page(url)
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=fetch):
            rows = batch.execute(runner.ShadowRunner(self.config_path, clock=lambda: NOW), self.root, plan,
                                 key_loader=lambda: KEY, transport=self.transport)
        self.assertEqual([r["outcome"] for r in rows], ["ok", "ok"])
        self.assertEqual(len(fetched), len(self.calls))
        self.assertEqual(len(self.calls), 2)
        path = next(self.root.glob("data/shadow/plans_*.jsonl"))
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertNotIn(KEY, path.read_text())
        from pbdata.collect.shadow.report import score_shadow
        output = next(self.root.glob("data/shadow/llm_extract_*.jsonl"))
        report = score_shadow([output], self.root, NOW, self.config,
                              ledger_path=self.root / "budget.sqlite3")
        self.assertEqual(report["planning"]["selected_urls"], 2)
        self.assertEqual(report["planning"]["selected_without_attempt"], 0)

    def _fetch_failing_first(self, fetched):
        def fetch(url):
            fetched.append(url)
            if len(fetched) == 1:
                raise common.Stop("fetch_error")
            return page(url)
        return fetch

    def test_proxy_outage_waits_and_retries_target(self):
        plan = planner.plan(export(*(candidate(gap_id=f"gap_{i}", url=f"https://example.org/{i}") for i in range(2))),
                            limit=2)
        fetched, waits = [], []
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=self._fetch_failing_first(fetched)), \
                patch.object(batch.socks_egress, "configured_proxy", return_value=("127.0.0.1", 1080)), \
                patch.object(batch.socks_egress, "preflight", side_effect=ConnectionRefusedError()):
            rows = batch.execute(runner.ShadowRunner(self.config_path, clock=lambda: NOW), self.root, plan,
                                 key_loader=lambda: KEY, transport=self.transport, proxy_wait=lambda: waits.append(1))
        self.assertEqual(waits, [1])
        self.assertEqual(rows[0].get("reason"), "fetch_error")
        self.assertTrue(rows[1].get("proxy_retry"))
        self.assertEqual([r["outcome"] for r in rows[1:]], ["ok", "ok"])
        self.assertEqual(len(fetched), 3)

    def test_proxy_still_down_stops_batch(self):
        plan = planner.plan(export(*(candidate(gap_id=f"gap_{i}", url=f"https://example.org/{i}") for i in range(3))),
                            limit=3)
        fetched = []
        def wait():
            raise ConnectionRefusedError()
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=self._fetch_failing_first(fetched)), \
                patch.object(batch.socks_egress, "configured_proxy", return_value=("127.0.0.1", 1080)), \
                patch.object(batch.socks_egress, "preflight", side_effect=ConnectionRefusedError()):
            rows = batch.execute(runner.ShadowRunner(self.config_path, clock=lambda: NOW), self.root, plan,
                                 key_loader=lambda: KEY, transport=self.transport, proxy_wait=wait)
        self.assertEqual([r.get("reason") for r in rows], ["fetch_error", "proxy_unavailable"])
        self.assertEqual(len(fetched), 1)
        self.assertEqual(self.calls, [])

    def test_site_fetch_error_with_healthy_proxy_continues_without_retry(self):
        plan = planner.plan(export(*(candidate(gap_id=f"gap_{i}", url=f"https://example.org/{i}") for i in range(2))),
                            limit=2)
        fetched = []
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=self._fetch_failing_first(fetched)), \
                patch.object(batch.socks_egress, "configured_proxy", return_value=("127.0.0.1", 1080)), \
                patch.object(batch.socks_egress, "preflight", return_value=("127.0.0.1", 1080)):
            rows = batch.execute(runner.ShadowRunner(self.config_path, clock=lambda: NOW), self.root, plan,
                                 key_loader=lambda: KEY, transport=self.transport,
                                 proxy_wait=lambda: self.fail("no wait when proxy is up"))
        self.assertEqual([r.get("reason") or r["outcome"] for r in rows], ["fetch_error", "ok"])
        self.assertEqual(len(fetched), 2)

    def test_key_in_plan_stops_before_persisting(self):
        plan = planner.plan(export(candidate(owner_name=KEY)))
        with self.assertRaisesRegex(common.Stop, "credential_in_record"):
            batch.execute(runner.ShadowRunner(self.config_path, clock=lambda: NOW), self.root, plan, key_loader=lambda: KEY)
        self.assertFalse(self.root.exists())


class FinalSafetyTests(Harness):
    def test_shared_concurrent_batch_and_single_ledger_with_halts(self):
        from concurrent.futures import ThreadPoolExecutor
        self.config["daily_call_cap"] = 3
        self.write_config()
        with state.State(self.root).locked():
            pass
        plan = planner.plan(export(*(candidate(gap_id=f"gap_{i}", url=f"https://example.org/{i}") for i in range(4))))
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=lambda url: page(url)), ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(self.run_page, url="https://example.org/single")
            many = pool.submit(batch.execute, runner.ShadowRunner(self.config_path, clock=lambda: NOW), self.root, plan,
                               key_loader=lambda: KEY, transport=self.transport)
            single, rows = one.result(), many.result()
        self.assertEqual(len(self.calls), 5)
        with state.State(self.root).locked() as c:
            self.assertEqual(c.execute("SELECT sum(provider_calls) FROM attempts").fetchone()[0], 5)
        self.assertEqual([r["outcome"] for r in [single] + rows], ["ok"] * 5)
        self.root = self.base / "halted"
        def mismatch(*args):
            self.calls.append(None)
            value = completion(); value["model"] = "unexpected"
            return value
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=lambda url: page(url)):
            rows = batch.execute(runner.ShadowRunner(self.config_path, clock=lambda: NOW), self.root, plan,
                                 key_loader=lambda: KEY, transport=mismatch)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "model_mismatch")

    def test_corrupt_matching_cache_never_refetched_and_denials_not_reused(self):
        first = self.run_page()
        target = planner.plan(export(candidate()))["targets"][0]
        path = self.root / "data/shadow/source_text" / (first["source_cache_key"] + ".json")
        path.write_text("bad cache")
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=AssertionError("refetch")):
            r = runner.ShadowRunner(self.config_path, clock=lambda: NOW).run(shadow_root=self.root, url=URL, target=target,
                    transport=self.transport, key_loader=lambda: KEY)
        self.assertEqual(r["reason"], "invalid_source_cache")
        self.assertEqual(len(self.calls), 1)
        target["targets"][0]["outcome"] = "denied_robots"
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=AssertionError("refetch")):
            r = runner.ShadowRunner(self.config_path, clock=lambda: NOW).run(shadow_root=self.root, url=URL, target=target,
                    transport=self.transport, key_loader=lambda: KEY)
        self.assertEqual(r["reason"], "ineligible_source")
        self.assertEqual(len(self.calls), 1)


# F1/F2: the service `content_hash` (latin-1 decode -> UTF-8 hash) is the shadow
# source identity for cache, history and source_changed. Korean bodies diverge
# from the raw-byte hash, so this is the contract-level regression.
KO_HTML = HTML.replace(b"</html>", "<p>한국어 안내 문구입니다.</p></html>".encode())


class ServiceHashContractTests(Harness):
    def test_korean_hash_reuse_history_coverage_and_digest(self):
        from pbdata.collect.shadow import source_cache
        from pbdata.collect.shadow.report import score_shadow
        body = KO_HTML
        service = common.source_content_hash(body)
        raw_hash = common.digest(body)
        self.assertNotEqual(service, raw_hash)
        p = page(body=body)
        self.assertEqual(p.source_content_hash, service)
        self.assertEqual(p.body_sha256, raw_hash)

        target = planner.plan(export(candidate(content_hash=service)))["targets"][0]
        first = self.run_page(page=p, target=target, digest_languages=("ko", "en"))
        self.assertEqual(first["outcome"], "ok")
        self.assertIs(first["source_changed"], False)
        self.assertEqual(first["source_content_hash"], service)
        self.assertEqual(first["body_sha256"], raw_hash)
        self.assertEqual({d["language"] for d in first["digest_drafts"]}, {"ko", "en"})

        # The bounded copy is discoverable through the service hash alone.
        cached = source_cache.load(self.root, URL, service, NOW)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.source_content_hash, service)
        self.assertEqual(cached.body_sha256, raw_hash)
        self.assertEqual(cached.doc(), p.doc())

        # Next UTC day: same service hash reuses the copy with zero page fetch.
        next_clock = NOW + dt.timedelta(days=1)
        next_target = planner.plan({"schema": planner.EXPORT_SCHEMA, "exported_at_utc": next_clock.isoformat(),
                                    "rows": [candidate(content_hash=service, fetched_at=next_clock.isoformat())]})["targets"][0]
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=AssertionError("refetch")), \
                patch("socket.socket", side_effect=AssertionError("network")):
            second = runner.ShadowRunner(self.config_path, clock=lambda: next_clock).run(
                shadow_root=self.root, url=URL, target=next_target,
                transport=self.transport, key_loader=lambda: KEY)
        self.assertEqual(second["outcome"], "ok")
        self.assertEqual(second["provenance"], "shadow_cached")
        self.assertIs(second["source_changed"], False)
        self.assertEqual(len(self.calls), 2)

        # Static-skip history matches on source_content_hash (config hash unknown
        # in plan-only mode still matches a completed static row).
        history = [dict(final_url=URL, body_sha256=raw_hash, source_content_hash=service, outcome="ok",
                        prompt_sha256=schema.PROMPT_HASH, schema_sha256=schema.SCHEMA_HASH,
                        config_sha256=common.digest(common.dumps(self.config)), timestamp_utc=NOW.isoformat())]
        for config_hash in (common.digest(common.dumps(self.config)), None):
            pending = planner.plan(export(candidate(content_hash=service)), history=history, config_hash=config_hash)
            self.assertFalse(pending["targets"])
            self.assertEqual(pending["reasons"]["skip_static_already_done"], 1)

        outputs = list(self.root.glob("data/shadow/llm_extract_*.jsonl"))
        # Counts/coverage below are ledger-independent; the snapshot needs 3.11.
        report = score_shadow(outputs, self.root, next_clock, self.config,
                              ledger_path=self.root / "budget.sqlite3")
        self.assertEqual(report["counts"]["verified_unique_pages"], 2)
        self.assertEqual(report["counts"]["changed_source_attempts"], 0)
        self.assertEqual(report["field_coverage"]["submission_fee"]["targeted_pages"], 2)

    def test_genuinely_changed_body_sets_source_changed(self):
        old, new = HTML, HTML.replace(b"December 1, 2026", b"December 2, 2026")
        target = planner.plan(export(candidate(content_hash=common.source_content_hash(old))))["targets"][0]
        result = self.run_page(page=page(body=new), target=target)
        self.assertEqual(result["outcome"], "ok")
        self.assertIs(result["source_changed"], True)
        self.assertEqual(result["source_content_hash"], common.source_content_hash(new))


class CacheIdentityTests(Harness):
    def test_final_url_collision_creates_a_distinct_snapshot(self):
        from pbdata.collect.shadow import policy, source_cache
        final_b = "https://example.org/final-b"
        p1 = policy.Page(URL, URL, HTML, (URL,), "offline_cached")
        p2 = policy.Page(URL, final_b, HTML, (URL, final_b), "offline_cached")
        self.assertEqual(p1.source_content_hash, p2.source_content_hash)
        st = state.State(self.root)
        with st.locked():
            pass
        name1 = source_cache.save(st, p1, p1.doc(), NOW, KEY)
        name2 = source_cache.save(st, p2, p2.doc(), NOW, KEY)
        self.assertNotEqual(name1, name2)
        self.assertEqual(source_cache.load(self.root, URL, p1.source_content_hash, NOW, final_url=URL).final_url, URL)
        self.assertEqual(source_cache.load(self.root, URL, p1.source_content_hash, NOW, final_url=final_b).final_url, final_b)


class LegacyScoreTests(unittest.TestCase):
    def test_legacy_heldout_rows_remain_readable(self):
        from pbdata.collect.shadow import compare
        raw = proposal(); raw.pop("submission_fee"); raw.pop("simultaneous")
        fields, _ = schema.validator("pb-shadow-1")(raw, page().doc(), "2026-11-05")
        item = compare.Item("fixture", URL, "2026-11-05", page(), page().body_sha256,
                            common.digest(page().doc().text()), "en", runner.baseline(page().doc(), "2026-11-05"))
        result = compare.score([item], {item.key: dict(schema="pb-shadow-1", fields=fields, text_sha256=item.text_hash)})
        self.assertEqual(result["summaries"]["fixture"]["field_coverage"]["submission_fee"], 0)


class PlanOnlyHistoryTests(Harness):
    def test_plan_only_reflects_prior_static_completion_without_writes(self):
        self.assertEqual(self.run_page()["outcome"], "ok")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        gaps = self.base / "gaps.json"
        gaps.write_text(common.dumps(export(candidate())))
        script = Path(__file__).resolve().parents[1] / "scripts/llm_extract_shadow_batch.py"
        result = subprocess.run([sys.executable, "-B", str(script), "--gaps-json", str(gaps),
                                 "--plan-only", "--allow-stale", "--shadow-root", str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["selected"], 0)
        self.assertEqual(output["reasons"]["skip_static_already_done"], 1)
        after = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


class ProjectMetadataTests(unittest.TestCase):
    def test_python_floor_supports_snapshot_fallback(self):
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        self.assertIn('requires-python = ">=3.9"', pyproject)


class OpenCallAuditTests(unittest.TestCase):
    NC = dict(is_real_call=False, status="not_call")

    def run_oc(self, value, quote, text, rejected):
        from pbdata.collect.parse import Doc, Segment
        raw = dict.fromkeys(schema.FIELDS)
        raw["open_call"] = dict(value=value, quote=quote)
        return schema.validate(raw, Doc("html", segments=[Segment(0, "p", text)]), "2026-11-05", rejected=rejected)

    def test_not_call_accepted_on_page_without_writing_context(self):
        q = "Graduate admissions are open for fall."
        rej = {}
        fields, reasons = self.run_oc(self.NC, q, q + " Tuition and housing information.", rej)
        self.assertIsNotNone(fields["open_call"])
        self.assertNotIn("open_call", reasons)
        self.assertEqual(rej, {})

    def test_not_call_still_rejected_on_writing_page_and_audited(self):
        q = "Our magazine publishes short stories and novels."
        text = " ".join([q] + ["Submit your story, poems and novels to our magazine."] * 12)
        rej = {}
        fields, reasons = self.run_oc(self.NC, q, text, rej)
        self.assertIsNone(fields["open_call"])
        self.assertEqual(reasons["open_call"], "unsupported_status")
        self.assertEqual(rej["open_call"], {"value": self.NC, "quote": q, "reason": "unsupported_status"})

    def test_quote_not_found_is_audited(self):
        rej = {}
        _, reasons = self.run_oc(self.NC, "missing quote", "Some other text", rej)
        self.assertEqual(reasons["open_call"], "quote_not_found")
        self.assertEqual(rej["open_call"]["reason"], "quote_not_found")
