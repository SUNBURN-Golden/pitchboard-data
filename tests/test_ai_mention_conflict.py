import dataclasses
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from helpers import NOW, add_call, add_outlet, add_revision, add_source, service
from pbdata import facts
from pbdata.daemon import project, stages
from test_daemon import Env
from test_facts import fact


class AIMentionConflictTests(unittest.TestCase):
    def setUp(self):
        self.c = service()
        self.addCleanup(self.c.close)
        self.outlet = add_outlet(self.c)
        self.call = add_call(self.c, self.outlet)
        self.sources = [add_source(self.c, "https://example.org/" + str(i)) for i in range(3)]
        self.revs = [add_revision(self.c, s) for s in self.sources]

    def ai(self, state, source=0, **kw):
        return fact(self.call, self.sources[source], self.revs[source], self.c, predicate="policy.ai",
                    value={"dimension": "generated_content", "state": state}, **kw)

    def legacy_group(self, **kw):
        a = facts.append(self.c, self.ai("mentioned_unclassified", **kw), NOW)
        b = facts.append(self.c, self.ai("prohibited", source=1, **kw), NOW)
        self.c.execute("INSERT INTO pb_conflict_group(group_id,subject_type,subject_id,predicate,scope_ref,kind,opened_at) "
                       "VALUES('cg_legacy',?,?,?,?,?,?)",
                       (kw.get("subject_type", "call"), kw.get("subject_id", self.call), "policy.ai", "book_review", "value_conflict", NOW))
        self.c.execute("UPDATE pb_fact SET conflict_group_id='cg_legacy' WHERE fact_id IN (?,?)", (a.fact_id, b.fact_id))
        return a, b

    def test_mention_and_prohibited_do_not_conflict_in_either_order(self):
        for states in (("mentioned_unclassified", "prohibited"), ("prohibited", "mentioned_unclassified")):
            with self.subTest(states=states):
                self.call = add_call(self.c, self.outlet, key=states[0])
                facts.append(self.c, self.ai(states[0]), NOW)
                result = facts.append(self.c, self.ai(states[1], source=1), NOW)
                self.assertIsNone(result.conflict_group_id)
                self.assertNotEqual(result.action, "conflict")
        self.assertEqual(self.c.execute("SELECT count(*) FROM pb_conflict_group").fetchone()[0], 0)

    def test_classified_disagreements_still_conflict(self):
        for state in ("allowed", "allowed_with_disclosure"):
            with self.subTest(state=state):
                self.call = add_call(self.c, self.outlet, key=state)
                facts.append(self.c, self.ai("prohibited"), NOW)
                result = facts.append(self.c, self.ai(state, source=1), NOW)
                self.assertEqual(result.action, "conflict")
                self.assertIsNotNone(result.conflict_group_id)
        self.assertEqual(facts.resolve_noncontradicting_conflicts(self.c, NOW), [])

    def test_same_source_mention_then_policy_supersedes(self):
        a = facts.append(self.c, self.ai("mentioned_unclassified"), NOW)
        b = facts.append(self.c, self.ai("prohibited"), NOW)
        self.assertEqual(b.action, "superseded_prior")
        self.assertEqual(b.superseded, [a.fact_id])
        self.assertIsNone(b.conflict_group_id)
        self.assertEqual(len(facts.live_facts(self.c, self.call)), 1)

    def test_resolver_records_decision_preserves_facts_and_updates_projection(self):
        self.legacy_group()
        before = [tuple(r) for r in self.c.execute("SELECT * FROM pb_fact ORDER BY fact_id")]
        dirty = self.c.execute("SELECT dirty_seq FROM pb_call_edition WHERE call_id=?", (self.call,)).fetchone()[0]
        self.assertEqual(project.build_payload(self.c, self.call)["conditions"]["policy.ai"]["answer"], "conflict")
        self.assertEqual(facts.resolve_noncontradicting_conflicts(self.c, NOW), ["cg_legacy"])
        self.assertEqual(before, [tuple(r) for r in self.c.execute("SELECT * FROM pb_fact ORDER BY fact_id")])
        group = self.c.execute("SELECT * FROM pb_conflict_group").fetchone()
        self.assertEqual(group["resolved_at"], NOW)
        decision = self.c.execute("SELECT * FROM pb_review_decision WHERE decision_id=?", (group["resolution_decision_id"],)).fetchone()
        self.assertEqual(decision["kind"], "resolve_conflict")
        self.assertEqual(decision["rationale"], "auto: non-committal mention does not contradict")
        self.assertEqual(len(json.loads(decision["data"])["active_fact_ids"]), 2)
        self.assertEqual(self.c.execute("SELECT dirty_seq FROM pb_call_edition WHERE call_id=?", (self.call,)).fetchone()[0], dirty + 1)
        cell = project.build_payload(self.c, self.call)["conditions"]["policy.ai"]
        self.assertEqual(cell["answer"], "stated")
        self.assertTrue(all(f["conflict_group_id"] is None for f in cell["facts"]))

    def test_resolver_keeps_three_member_group_with_real_disagreement(self):
        self.legacy_group()
        result = facts.append(self.c, self.ai("allowed", source=2), NOW)
        self.assertEqual(result.conflict_group_id, "cg_legacy")
        self.assertEqual(facts.resolve_noncontradicting_conflicts(self.c, NOW), [])
        self.assertIsNone(self.c.execute("SELECT resolved_at FROM pb_conflict_group").fetchone()[0])
        self.assertEqual(self.c.execute("SELECT count(*) FROM pb_review_decision").fetchone()[0], 0)

    def test_resolver_is_idempotent(self):
        self.legacy_group()
        facts.resolve_noncontradicting_conflicts(self.c, NOW)
        before = list(self.c.iterdump())
        self.assertEqual(facts.resolve_noncontradicting_conflicts(self.c, "2026-11-18T00:00:00Z"), [])
        self.assertEqual(list(self.c.iterdump()), before)

    def test_non_ai_predicate_with_same_state_names_still_conflicts(self):
        facts.append(self.c, dataclasses.replace(self.ai("mentioned_unclassified"), predicate="rights.statement"), NOW)
        result = facts.append(self.c, dataclasses.replace(self.ai("prohibited", source=1), predicate="rights.statement"), NOW)
        self.assertEqual(result.action, "conflict")
        self.assertEqual(facts.resolve_noncontradicting_conflicts(self.c, NOW), [])

    def test_different_ai_dimensions_are_unaffected(self):
        facts.append(self.c, self.ai("mentioned_unclassified"), NOW)
        f = dataclasses.replace(self.ai("prohibited", source=1), value={"dimension": "editing", "state": "prohibited"})
        self.assertEqual(facts.append(self.c, f, NOW).action, "conflict")
        self.assertEqual(facts.resolve_noncontradicting_conflicts(self.c, NOW), [])

    def test_resolver_ignores_superseded_members(self):
        self.legacy_group()
        facts.append(self.c, self.ai("allowed", source=2), NOW)
        facts.append(self.c, self.ai("prohibited", source=2), NOW)
        self.assertEqual(facts.resolve_noncontradicting_conflicts(self.c, NOW), ["cg_legacy"])

    def test_new_disagreement_after_resolution_stays_open(self):
        self.legacy_group()
        facts.resolve_noncontradicting_conflicts(self.c, NOW)
        result = facts.append(self.c, self.ai("allowed", source=2), NOW)
        self.assertEqual(result.action, "conflict")
        self.assertNotEqual(result.conflict_group_id, "cg_legacy")
        self.assertEqual(facts.resolve_noncontradicting_conflicts(self.c, NOW), [])
        self.assertIsNone(self.c.execute("SELECT resolved_at FROM pb_conflict_group WHERE group_id=?",
                                         (result.conflict_group_id,)).fetchone()[0])

    def test_outlet_resolution_marks_dependent_calls_dirty(self):
        self.legacy_group(subject_type="outlet", subject_id=self.outlet)
        dirty = self.c.execute("SELECT dirty_seq FROM pb_call_edition WHERE call_id=?", (self.call,)).fetchone()[0]
        facts.resolve_noncontradicting_conflicts(self.c, NOW)
        self.assertEqual(self.c.execute("SELECT dirty_seq FROM pb_call_edition WHERE call_id=?", (self.call,)).fetchone()[0], dirty + 1)

    def test_daemon_resolves_before_projection_and_quality_check(self):
        self.legacy_group()
        e = Env(now=NOW)
        self.addCleanup(e.close)
        c = e.ctx()
        c.svc = self.c
        self.assertEqual(stages.s_service_recompute(c)["conflicts_resolved"], 1)
        q = stages.s_quality_check(c)
        self.assertEqual(q["conflicts_open"], 0)
        self.assertEqual(q["projection_behind_dirty"], 0)
        self.assertEqual(q["violations"], [])
        payload = json.loads(self.c.execute("SELECT payload FROM pb_search_projection WHERE call_id=?", (self.call,)).fetchone()[0])
        self.assertEqual(payload["conditions"]["policy.ai"]["answer"], "stated")

    def test_eval_copies_database_and_preserves_original(self):
        self.legacy_group()
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "original.sqlite3"
            with sqlite3.connect(path) as dest:
                self.c.backup(dest)
            before = path.read_bytes()
            result = subprocess.run([sys.executable, str(root / "scripts/eval_conflict_resolution.py"), str(path), "--now", NOW],
                                    check=True, capture_output=True, text=True)
            report = json.loads(result.stdout)
            self.assertEqual(report["conflicts_open"], {"before": 1, "after": 0})
            self.assertEqual(report["resolved_groups"][0]["predicate"], "policy.ai")
            self.assertEqual(report["remaining_groups"], [])
            self.assertTrue(report["fact_integrity"]["unchanged_except_conflict_linkage"])
            self.assertEqual(path.read_bytes(), before)
