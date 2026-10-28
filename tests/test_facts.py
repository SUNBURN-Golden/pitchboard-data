import sqlite3
import unittest

from helpers import NOW, add_call, add_outlet, add_revision, add_source, service
from pbdata import facts, statemachine
from pbdata.ids import sha256_hex
from pbdata.model import FactInput, ValidationError


def fact(call, src, rev, conn, predicate="fee.submission", value=None, state="stated", epi="source_claim",
         excerpt="There is no submission fee.", **kw):
    ev = facts.add_evidence(conn, rev, src, excerpt, "Guidelines", 0, 0, NOW)
    base = dict(subject_type="call", subject_id=call, predicate=predicate, source_id=src, value_state=state, epistemic=epi,
                collected_at=NOW, extractor="rules", extractor_version="1", scope_ref="book_review", evidence_id=ev,
                source_authority="official_primary", value=value if value is not None else {"amount_min": 0})
    base.update(kw)
    return FactInput(**base)


class FactTests(unittest.TestCase):
    def setUp(self):
        self.c = service()
        self.o = add_outlet(self.c)
        self.call = add_call(self.c, self.o)
        self.s1 = add_source(self.c, "https://example.org/a")
        self.s2 = add_source(self.c, "https://example.net/b", authority="attributed_secondary", max_epistemic="source_claim")
        self.r1 = add_revision(self.c, self.s1)
        self.r2 = add_revision(self.c, self.s2)

    def test_v01_stated_needs_evidence(self):
        f = FactInput("call", self.call, "fee.submission", self.s1, "stated", "source_claim", NOW, "rules", "1", value={"x": 1})
        with self.assertRaises(ValidationError):
            facts.append(self.c, f, NOW)

    def test_v02_verified_needs_everything(self):
        f = fact(self.call, self.s1, self.r1, self.c, epi="verified_fact")
        with self.assertRaises(ValidationError) as cm:
            facts.append(self.c, f, NOW)
        self.assertIn("V-02", str(cm.exception))
        f = fact(self.call, self.s1, self.r1, self.c, epi="verified_fact", verifier="rules", verified_at=NOW,
                 fresh_until="2026-11-27T00:00:00Z", state="explicitly_none")
        self.assertEqual(facts.append(self.c, f, NOW).action, "created")

    def test_v03_explicit_none_requires_wording(self):
        f = fact(self.call, self.s1, self.r1, self.c, state="explicitly_none", excerpt="We read all manuscripts.")
        with self.assertRaises(ValidationError) as cm:
            facts.append(self.c, f, NOW)
        self.assertIn("V-03", str(cm.exception))

    def test_v05_money_comparable_flag(self):
        bad = fact(self.call, self.s1, self.r1, self.c, predicate="compensation.rate", excerpt="We pay $100.",
                   value={"amount_min": 100, "currency": "USD", "unit": None, "comparable": True})
        with self.assertRaises(ValidationError):
            facts.append(self.c, bad, NOW)
        ok = fact(self.call, self.s1, self.r1, self.c, predicate="compensation.rate", excerpt="We pay $100.",
                  value={"amount_min": 100, "currency": "USD", "unit": None, "comparable": False})
        self.assertEqual(facts.append(self.c, ok, NOW).action, "created")

    def test_v06_ambiguous_date_rejected(self):
        f = fact(self.call, self.s1, self.r1, self.c, predicate="deadline.closes_on", excerpt="Deadline 04/05/2026.",
                 value={"date": "04/05/2026", "tz_source": "unknown"})
        with self.assertRaises(ValidationError) as cm:
            facts.append(self.c, f, NOW)
        self.assertIn("V-06", str(cm.exception))

    def test_v10_pii_redacted_in_evidence(self):
        eid = facts.add_evidence(self.c, self.r1, self.s1, "Send to editor@example.org or 010-1234-5678 now.", "h", 1, 0, NOW)
        text = self.c.execute("SELECT excerpt FROM pb_evidence WHERE evidence_id=?", (eid,)).fetchone()[0]
        self.assertNotIn("@", text)
        self.assertIn("[email]", text)
        self.assertIn("[phone]", text)

    def test_v13_not_collectable_source_rejected_but_historic_allowed(self):
        s = add_source(self.c, "https://blocked.example/x", permission="not_collectable")
        r = add_revision(self.c, s)
        f = fact(self.call, s, r, self.c)
        with self.assertRaises(ValidationError) as cm:
            facts.append(self.c, f, NOW)
        self.assertIn("V-13", str(cm.exception))
        self.assertEqual(facts.append(self.c, f, NOW, historic=True).action, "created")

    def test_v13_epistemic_ceiling_for_leads(self):
        s = add_source(self.c, "https://social.example/p", permission="manual_only", max_epistemic="unverified", source_type="social_lead")
        r = add_revision(self.c, s)
        f = fact(self.call, s, r, self.c, epi="source_claim")
        with self.assertRaises(ValidationError):
            facts.append(self.c, f, NOW)
        f2 = fact(self.call, s, r, self.c, epi="unverified")
        self.assertEqual(facts.append(self.c, f2, NOW).action, "created")

    def test_v07_same_source_edit_supersedes_keeps_history(self):
        a = facts.append(self.c, fact(self.call, self.s1, self.r1, self.c, predicate="deadline.closes_on", excerpt="Deadline Oct 31, 2026.",
                                      value={"date": "2026-10-31", "tz_source": "unknown"}), NOW)
        r1b = add_revision(self.c, self.s1, "h2")
        b = facts.append(self.c, fact(self.call, self.s1, r1b, self.c, predicate="deadline.closes_on", excerpt="Deadline Nov 15, 2026.",
                                      value={"date": "2026-11-15", "tz_source": "unknown"}), NOW)
        self.assertEqual(b.action, "superseded_prior")
        hist = facts.fact_history(self.c, self.call, "deadline.closes_on")
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["superseded_by"], b.fact_id)
        self.assertEqual(len(facts.live_facts(self.c, self.call)), 1)

    def test_v07_cross_source_conflict_kept_not_overwritten(self):
        a = facts.append(self.c, fact(self.call, self.s1, self.r1, self.c, predicate="deadline.closes_on", excerpt="Deadline Dec 15, 2026.",
                                      value={"date": "2026-12-15", "tz_source": "unknown"}, epi="verified_fact", verifier="rules",
                                      verified_at=NOW, fresh_until="2026-11-27T00:00:00Z"), NOW)
        b = facts.append(self.c, fact(self.call, self.s2, self.r2, self.c, predicate="deadline.closes_on", excerpt="Deadline: December 1, 2026.",
                                      value={"date": "2026-12-01", "tz_source": "unknown"}, source_authority="attributed_secondary"), NOW)
        self.assertEqual(b.action, "conflict")
        live = facts.live_facts(self.c, self.call)
        self.assertEqual(len(live), 2)
        self.assertTrue(all(r["conflict_group_id"] for r in live))
        self.assertNotIn("verified_fact", [r["epistemic"] for r in live])  # demoted by append, original retained
        self.assertEqual(len(facts.fact_history(self.c, self.call, "deadline.closes_on")), 3)
        kind = self.c.execute("SELECT kind FROM pb_conflict_group").fetchone()[0]
        self.assertEqual(kind, "value_conflict")

    def test_lead_discrepancy_does_not_demote_official(self):
        lead = add_source(self.c, "https://social.example/p", permission="manual_only", max_epistemic="unverified", source_type="social_lead")
        rl = add_revision(self.c, lead)
        facts.append(self.c, fact(self.call, self.s1, self.r1, self.c, predicate="deadline.closes_on", excerpt="Deadline Oct 31, 2026.",
                                  value={"date": "2026-10-31", "tz_source": "unknown"}, epi="verified_fact", verifier="rules",
                                  verified_at=NOW, fresh_until="2026-11-27T00:00:00Z"), NOW)
        res = facts.append(self.c, fact(self.call, lead, rl, self.c, predicate="deadline.closes_on", excerpt="Extended to Nov 20, 2026!",
                                        value={"date": "2026-11-20", "tz_source": "unknown"}, epi="unverified",
                                        source_authority="discovery_lead"), NOW)
        self.assertEqual(self.c.execute("SELECT kind FROM pb_conflict_group").fetchone()[0], "lead_discrepancy")
        epis = sorted(r["epistemic"] for r in facts.live_facts(self.c, self.call))
        self.assertEqual(epis, ["unverified", "verified_fact"])

    def test_reconfirmation_extends_freshness_without_rewrite(self):
        f = fact(self.call, self.s1, self.r1, self.c, epi="verified_fact", verifier="rules", verified_at=NOW,
                 fresh_until="2026-11-03T00:00:00Z", state="explicitly_none")
        a = facts.append(self.c, f, NOW)
        r2 = add_revision(self.c, self.s1, "h9")
        again = facts.append(self.c, fact(self.call, self.s1, r2, self.c, epi="source_claim", state="explicitly_none",
                                          collected_at="2026-11-01T00:00:00Z"), "2026-11-01T00:00:00Z")
        self.assertEqual(again.action, "unchanged")
        self.assertEqual(again.fact_id, a.fact_id)
        self.assertGreater(facts.effective_fresh_until(self.c, a.fact_id), "2026-11-03T00:00:00Z")

    def test_v08_append_only_triggers(self):
        a = facts.append(self.c, fact(self.call, self.s1, self.r1, self.c), NOW)
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute("UPDATE pb_fact SET value_json='{}' WHERE fact_id=?", (a.fact_id,))
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute("DELETE FROM pb_fact WHERE fact_id=?", (a.fact_id,))

    def test_correction_requires_reason_and_keeps_old(self):
        a = facts.append(self.c, fact(self.call, self.s1, self.r1, self.c), NOW)
        res = facts.correct_fact(self.c, a.fact_id, fact(self.call, self.s1, self.r1, self.c, value={"amount_min": 5}), "typo on page", NOW)
        new = self.c.execute("SELECT correction_of, correction_reason FROM pb_fact WHERE fact_id=?", (res.fact_id,)).fetchone()
        self.assertEqual(new["correction_of"], a.fact_id)

    def test_conflict_resolution_needs_review_decision(self):
        facts.append(self.c, fact(self.call, self.s1, self.r1, self.c, predicate="deadline.closes_on", excerpt="Deadline Dec 15, 2026.",
                                  value={"date": "2026-12-15", "tz_source": "unknown"}), NOW)
        res = facts.append(self.c, fact(self.call, self.s2, self.r2, self.c, predicate="deadline.closes_on", excerpt="Deadline: December 1, 2026.",
                                        value={"date": "2026-12-01", "tz_source": "unknown"}), NOW)
        with self.assertRaises(ValidationError):
            facts.resolve_conflict(self.c, res.conflict_group_id, "nope", None, NOW)
        self.c.execute("INSERT INTO pb_review_decision VALUES('rd1','resolve_conflict','operator','official page wins',?, '{}')", (NOW,))
        keep = [r["fact_id"] for r in facts.live_facts(self.c, self.call) if r["source_id"] == self.s1][0]
        facts.resolve_conflict(self.c, res.conflict_group_id, "rd1", keep, NOW)
        self.assertEqual(len(facts.live_facts(self.c, self.call)), 1)
        self.assertEqual(len(facts.fact_history(self.c, self.call, "deadline.closes_on")), 2)  # nothing deleted


class StateTests(unittest.TestCase):
    def setUp(self):
        self.c = service()
        self.call = add_call(self.c, add_outlet(self.c))

    def test_requires_evidence_basis_and_allowed_edges(self):
        with self.assertRaises(ValidationError):
            statemachine.transition(self.c, self.call, "open", "evidence", NOW, "t", "x")  # no evidence
        with self.assertRaises(ValidationError):
            statemachine.transition(self.c, self.call, "open", "system_clock", NOW, "t", "x")  # candidate->open not by clock
        s = add_source(self.c)
        r = add_revision(self.c, s)
        ev = facts.add_evidence(self.c, r, s, "Submissions are open.", "h", 0, 0, NOW)
        statemachine.transition(self.c, self.call, "open", "evidence", NOW, "t", "declared open", evidence_id=ev)
        statemachine.transition(self.c, self.call, "closed", "system_clock", NOW, "system", "deadline_passed")
        with self.assertRaises(ValidationError):
            statemachine.transition(self.c, self.call, "cancelled", "system_clock", NOW, "system", "x")  # never by clock
        statemachine.transition(self.c, self.call, "open", "evidence", NOW, "t", "reopened", evidence_id=ev)
        self.assertEqual(self.c.execute("SELECT count(*) FROM pb_status_event").fetchone()[0], 3)

    def test_cancelled_cannot_reopen(self):
        s = add_source(self.c); r = add_revision(self.c, s)
        ev = facts.add_evidence(self.c, r, s, "This call has been cancelled.", "h", 0, 0, NOW)
        statemachine.transition(self.c, self.call, "cancelled", "evidence", NOW, "t", "c", evidence_id=ev)
        with self.assertRaises(ValidationError):
            statemachine.transition(self.c, self.call, "open", "evidence", NOW, "t", "x", evidence_id=ev)


class MigrationTests(unittest.TestCase):
    def test_additive_only_lint_and_checksums(self):
        from pbdata import db
        with self.assertRaises(db.MigrationError):
            db.lint_migration("DROP TABLE pb_fact;")
        with self.assertRaises(db.MigrationError):
            db.lint_migration("DELETE FROM pb_fact;")
        db.lint_migration("CREATE TABLE x(a); -- DROP TABLE comment ok")
        c = service()
        self.assertEqual(db.migrate(c, "service"), [])
        c.execute("UPDATE schema_migrations SET checksum='bad'")
        with self.assertRaises(db.MigrationError):
            db.migrate(c, "service")
