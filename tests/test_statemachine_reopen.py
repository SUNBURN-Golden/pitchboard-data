"""cancelled -> open is a review-only correction (wrongly extracted cancellations)."""
import unittest

from helpers import NOW, service, add_outlet, add_call
from pbdata import statemachine as sm
from pbdata.model import ValidationError


class ReopenCancelledTest(unittest.TestCase):
    def setUp(self):
        self.conn = service()
        self.cid = add_call(self.conn, add_outlet(self.conn), status="cancelled")

    def test_review_can_reopen_cancelled(self):
        eid = sm.transition(self.conn, self.cid, "open", sm.REV, NOW, "reviewer", "extractor bug fixed in PR #11")
        row = self.conn.execute("SELECT status, status_basis FROM pb_call_edition WHERE call_id=?", (self.cid,)).fetchone()
        self.assertEqual((row["status"], row["status_basis"]), ("open", "review"))
        ev = self.conn.execute("SELECT from_status,to_status,basis FROM pb_status_event WHERE event_id=?", (eid,)).fetchone()
        self.assertEqual(tuple(ev), ("cancelled", "open", "review"))

    def test_evidence_and_clock_cannot_reopen_cancelled(self):
        for basis in (sm.EV, sm.CLK):
            with self.assertRaises(ValidationError):
                sm.transition(self.conn, self.cid, "open", basis, NOW, "x", "y", evidence_id="ev_x")
        self.assertEqual(self.conn.execute("SELECT status FROM pb_call_edition WHERE call_id=?", (self.cid,)).fetchone()[0], "cancelled")


if __name__ == "__main__":
    unittest.main()
