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
