"""Synthetic shadow improvement checks; no live inputs or provider calls."""
import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import pytest
from pbdata.collect.shadow import batch, common, planner, runner, schema, state
from test_llm_extract import Harness, NOW, URL, KEY, page, proposal, completion


def candidate(**changes):
    row = dict(gap_id="gap_1", subject_type="outlet", subject_id="out_1", predicate="fee.submission",
               scope_ref="unspecified", state="open", gap_class="not_extracted", importance=.7,
               owner_outlet_id="out_1", source_id="src_1", owner_name="Example", outlet_name="Example",
               permission="allowed_public", raw_id="raw_1", url=URL, final_url=URL, kind="html", outcome="ok",
               http_status=200, content_hash=common.digest(page().body), text_hash=None,
               fetched_at="2026-11-05T00:00:00Z", relevance=0, relationship_basis="outlet_source")
    row.update(changes)
    return row


def export(*rows):
    return dict(schema=planner.EXPORT_SCHEMA, exported_at_utc=NOW.isoformat(), rows=list(rows))


def test_plan_priority_dedup_exclusions_latest():
    rows = [candidate(), candidate(gap_id="gap_2", predicate="compensation.rate"),
            candidate(gap_id="gap_3", url="https://example.net/deadline", predicate="deadline.closes_on"),
            candidate(gap_id="gap_4", predicate="rights.simultaneous"),
            candidate(gap_id="gap_5", owner_name="SOULBOUND", url="https://other.org/"),
            candidate(gap_id="gap_6", url="https://instagram.com/a"),
            candidate(gap_id="gap_7", owner_outlet_id=None),
            candidate(gap_id="gap_8", url="https://denied.org/", raw_id="raw_z"),
            candidate(gap_id="gap_8", url="https://denied.org/", raw_id="raw_a", fetched_at="2026-11-05T01:00:00Z", outcome="denied_robots")]
    with patch("socket.socket", side_effect=AssertionError("network")):
        plan = planner.plan(export(*rows))
    assert [t["url"] for t in plan["targets"]] == [URL, "https://example.net/deadline"]
    assert len(plan["targets"][0]["targets"]) == 2
    assert set(plan["reasons"]) == {"unsupported_predicate", "excluded_owner", "blocked_host", "no_outlet_relation", "superseded_observation", "ineligible_source"}
    many = export(*(candidate(gap_id=f"gap_{i}", url=f"https://example.org/{i}") for i in range(60)))
    assert len(planner.plan(many)["targets"]) == 40
    with pytest.raises(common.Stop, match="invalid_limit"):
        planner.plan(many, limit=51)


def test_stale_and_static_history(tmp_path):
    p = tmp_path / "gaps.json"
    p.write_text(common.dumps(export(candidate())))
    with pytest.raises(common.Stop, match="stale_gap_export"):
        planner.read_json(p, NOW + dt.timedelta(days=2))
    assert planner.read_json(p, NOW + dt.timedelta(days=2), allow_stale=True)
    h = dict(outcome="ok", final_url=URL, body_sha256=candidate()["content_hash"],
             prompt_sha256=schema.PROMPT_HASH, schema_sha256=schema.SCHEMA_HASH, config_sha256="config")
    assert not planner.plan(export(candidate()), history=[h], config_hash="config")["targets"]
    assert planner.plan(export(candidate(predicate="status.declared")), history=[h], config_hash="config")["targets"]


def test_db_snapshot_parity_and_no_writes(tmp_path):
    p = tmp_path / "fixture.sqlite"
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
    before = (p.read_bytes(), p.stat().st_mtime_ns, set(tmp_path.iterdir()))
    assert planner.plan(planner.read_db(p, NOW)) == planner.plan(export(r))
    assert before == (p.read_bytes(), p.stat().st_mtime_ns, set(tmp_path.iterdir()))
    Path(str(p) + "-wal").touch()
    with pytest.raises(common.Stop, match="db_requires_export"):
        planner.read_db(p, NOW)


def test_cli_disabled_and_plan_only(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/llm_extract_shadow_batch.py"
    p = tmp_path / "gaps.json"
    p.write_text(common.dumps(export(candidate())))
    root = tmp_path / "shadow"
    args = [sys.executable, "-B", str(script), "--gaps-json", str(p), "--shadow-root", str(root)]
    result = subprocess.run(args, capture_output=True, text=True)
    assert json.loads(result.stdout)["reason"] == "disabled"
    assert not root.exists()
    result = subprocess.run(args + ["--plan-only", "--allow-stale"], capture_output=True, text=True)
    assert json.loads(result.stdout)["selected"] == 1
    assert not root.exists()


def test_unknown_results_stop():
    for r in ({"outcome": "surprise"}, {"outcome": "ok", "reason": "experiment_halted"},
              {"outcome": "credential_in_response"}, None):
        assert batch.terminal(r)
    assert not batch.terminal({"outcome": "provider_error"})


class BatchTests(Harness):
    def test_cap_before_fetch_and_single_shared_ledger(self):
        self.config["daily_call_cap"] = 1
        self.write_config()
        assert self.run_page()["outcome"] == "ok"
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=AssertionError("fetch")):
            r = runner.ShadowRunner(self.config_path, clock=lambda: NOW).run(shadow_root=self.root, url=URL)
        assert r["reason"] == "budget_exhausted"


class CacheTests(Harness):
    def test_matching_copy_no_fetch_baseline_roundtrip_and_corruption(self):
        from pbdata.collect.shadow import source_cache
        first = self.run_page()
        assert first["outcome"] == "ok"
        cached = source_cache.load(self.root, URL, page().body_sha256, NOW)
        assert cached.document == page().doc()
        assert runner.baseline(cached.doc(), "2026-11-05") == first["baseline"]
        assert cached.provenance == "shadow_cached"
        cached.provenance = "live_verified"
        with pytest.raises(common.Stop, match="invalid_policy_envelope"):
            cached.doc()
        target = planner.plan(export(candidate()))["targets"][0]
        with patch.object(runner.PolicyFetcher, "fetch", side_effect=AssertionError("refetch")), patch("socket.socket", side_effect=AssertionError("network")):
            r = runner.ShadowRunner(self.config_path, clock=lambda: NOW).run(shadow_root=self.root, url=URL, target=target,
                    transport=self.transport, key_loader=lambda: KEY)
        assert r["outcome"] == "ok"
        assert r["provenance"] == "shadow_cached"
        path = self.root / "data/shadow/source_text" / (first["source_cache_key"] + ".json")
        raw = json.loads(path.read_text()); raw["payload"]["doc"]["title"] = "changed"
        path.write_text(common.dumps(raw))
        with pytest.raises(common.Stop, match="corrupt_source_cache"):
            source_cache.load(self.root, URL, page().body_sha256, NOW)

    def test_missing_null_oversize_expired_and_save_failure_no_spend(self):
        from pbdata.collect.shadow import source_cache
        assert source_cache.load(self.root, URL, None, NOW) is None
        with patch.object(source_cache, "save", side_effect=common.Stop("unsafe_path")):
            assert self.run_page()["reason"] == "unsafe_path"
        with state.State(self.root).locked() as c:
            assert c.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        self.run_page()
        with pytest.raises(common.Stop, match="page_too_large"):
            source_cache.load(self.root, URL, page().body_sha256, NOW, limit=10)
        assert source_cache.load(self.root, URL, page().body_sha256, NOW + dt.timedelta(days=30)) is None
        assert KEY.encode() not in self.artifacts()

    def test_cache_preserves_table_metadata_and_utf8(self):
        from pbdata.collect.shadow import source_cache
        p = page(body='<html lang="ko"><title>모집</title><meta property="og:title" content="시 모집"><table><tr><td>응모료</td><td>10,000원</td></tr></table></html>'.encode())
        r = self.run_page(page=p)
        assert r["outcome"] == "ok"
        cached = source_cache.load(self.root, URL, p.body_sha256, NOW)
        assert cached.doc().segments == p.doc().segments
        assert cached.doc().meta == p.doc().meta
        assert runner.baseline(cached.doc(), r["as_of"]) == r["baseline"]


def validate_one(field, value, quote, text=None):
    from pbdata.collect.parse import Doc, Segment
    raw = dict.fromkeys(schema.FIELDS)
    raw[field] = dict(value=value, quote=quote)
    return schema.validate(raw, Doc("html", segments=[Segment(0, "p", text or quote)]), "2026-11-05")


def fee(text="Entry fee USD 20", **changes):
    value = dict(text=text, state="stated", amount_min="20", amount_max="20", currency="USD", unit="per_submission")
    value.update(changes)
    return value


def test_strict_tokens_fee_date_currency_unit_and_negation():
    q = "Entry fee USD 20"
    assert validate_one("submission_fee", fee(), q)[0]["submission_fee"]
    for value in (fee(amount_min="2"), fee(currency="KRW"), fee(unit="per_word"), fee(text="invented")):
        assert validate_one("submission_fee", value, q)[0]["submission_fee"] is None
    for q in ("No entry fee is stated.", "There is a prize USD 20."):
        assert validate_one("submission_fee", fee(text=q), q)[0]["submission_fee"] is None
    q = "No submission fee."
    assert validate_one("submission_fee", fee(text=q, state="explicitly_none", amount_min=None, amount_max=None, currency=None, unit=None), q)[0]["submission_fee"]
    for q, amount in (("응모료 10,000원", "10000"), ("응모료 ２만원", "20000"), ("Entry fee USD 1,000.50", "1000.50")):
        v = fee(text=q, amount_min=amount, amount_max=amount, currency="KRW" if "원" in q else "USD")
        assert validate_one("submission_fee", v, q)[0]["submission_fee"]
    q = "Entry fee USD 2026"
    assert validate_one("submission_fee", fee(text=q), q)[1]["submission_fee"] == "unsupported_amount"
    q = "Submissions close December 1, 2026."
    v = dict(text="December 1, 2026", date="2026-12-02", timezone=None)
    assert validate_one("deadline", v, q)[1]["deadline"] == "unsupported_date"
    v.update(date="2026-12-01", timezone="KST")
    assert validate_one("deadline", v, q)[1]["deadline"] == "unsupported_timezone"
    v = dict(is_real_call=True, status="open")
    assert validate_one("open_call", v, "We are not currently open to submissions.")[0]["open_call"] is None
    assert validate_one("open_call", dict(is_real_call=False, status="not_call"), "Submissions are open.")[0]["open_call"] is None
    raw = proposal(); raw.pop("submission_fee")
    assert schema.validator("pb-shadow-1")(raw, page().doc(), "2026-11-05")[0]["pay"]


class ReviewTests(Harness):
    def test_conflicts_flags_missing_baseline_and_recover_once(self):
        from pbdata.collect.shadow.review import reviews
        row = self.run_page()
        changed = copy.deepcopy(row)
        changed["fields"]["deadline"]["value"]["date"] = "2026-12-02"
        assert any("field_conflict" in r["reasons"] for r in reviews(changed))
        changed = copy.deepcopy(row)
        changed["fields"]["open_call"]["value"] = dict(is_real_call=False, status="not_call")
        assert any(r["second_opinion_required"] for r in reviews(changed))
        changed = copy.deepcopy(row)
        changed["baseline"].update(current_open=False, real_call=False, labels=[dict(status="results_not_call")])
        assert any(r["second_opinion_required"] for r in reviews(changed))
        changed["baseline"].update(labels=[], extensions=[], deadline=[])
        rs = reviews(changed)
        assert not any(r["second_opinion_required"] for r in rs)
        assert any("baseline_uncertain" in r["reasons"] for r in rs)
        # Missing baseline fields produce candidate fills, never conflicts.
        assert any(r["reasons"] == ["candidate_fill"] for r in rs)
        # A failure publishing derived rows happens after the completed attempt
        # is durable; recovery must publish them without another provider call.
        self.root = self.base / "recover"
        with patch.object(state.State, "publish_derived", side_effect=OSError("failure")), patch.object(runner, "baseline", return_value=changed["baseline"]):
            assert self.run_page()["outcome"] == "skipped"
        with state.State(self.root).locked():
            pass
        paths = list((self.root / "data/shadow").glob("review_conflicts_*.jsonl"))
        assert paths
        before = [p.read_bytes() for p in paths]
        with state.State(self.root).locked():
            pass
        assert before == [p.read_bytes() for p in paths]
        assert len(self.calls) == 2
        assert KEY.encode() not in self.artifacts()
