"""Call-edition state machine (docs/DESIGN.md section 3). Stored status changes only through evidence-bearing events."""
from __future__ import annotations

import sqlite3
from typing import Optional

from .db import transaction
from .ids import new_id
from .model import ValidationError, Violation
from . import timeutil

EV, CLK, REV = "evidence", "system_clock", "review"
ALLOWED = {
    ("candidate", "announced"): {EV}, ("candidate", "open"): {EV}, ("candidate", "standing"): {EV},
    ("candidate", "closed"): {EV}, ("candidate", "cancelled"): {EV}, ("candidate", "superseded"): {REV},
    ("announced", "open"): {EV, CLK}, ("announced", "closed"): {EV}, ("announced", "cancelled"): {EV},
    ("announced", "status_unknown"): {CLK}, ("announced", "superseded"): {REV},
    ("open", "extended"): {EV}, ("open", "closed"): {EV, CLK}, ("open", "cancelled"): {EV},
    ("open", "status_unknown"): {CLK}, ("open", "superseded"): {REV},
    ("extended", "extended"): {EV}, ("extended", "closed"): {EV, CLK}, ("extended", "cancelled"): {EV},
    ("extended", "status_unknown"): {CLK}, ("extended", "superseded"): {REV},
    ("standing", "closed"): {EV}, ("standing", "cancelled"): {EV}, ("standing", "status_unknown"): {CLK},
    ("standing", "superseded"): {REV},
    ("closed", "open"): {EV}, ("closed", "extended"): {EV}, ("closed", "archived"): {CLK}, ("closed", "superseded"): {REV},
    ("cancelled", "archived"): {CLK}, ("cancelled", "superseded"): {REV},
    ("status_unknown", "announced"): {EV}, ("status_unknown", "open"): {EV}, ("status_unknown", "extended"): {EV},
    ("status_unknown", "standing"): {EV}, ("status_unknown", "closed"): {EV}, ("status_unknown", "cancelled"): {EV},
    ("superseded", "archived"): {CLK},
}


def transition(conn: sqlite3.Connection, call_id: str, to_status: str, basis: str, now: str, actor: str, reason: str,
               evidence_id: Optional[str] = None, fact_id: Optional[str] = None, kind: str = "transition") -> str:
    """V-14: reject transitions outside the table or without the required basis; never time-only cancel."""
    with transaction(conn):
        row = conn.execute("SELECT status FROM pb_call_edition WHERE call_id=?", (call_id,)).fetchone()
        if row is None:
            raise ValidationError([Violation("V-14", "unknown call")])
        cur = row["status"]
        if basis not in ALLOWED.get((cur, to_status), set()):
            raise ValidationError([Violation("V-14", "transition %s->%s not allowed with basis %s" % (cur, to_status, basis))])
        if basis == EV and not (evidence_id or fact_id):
            raise ValidationError([Violation("V-14", "evidence transition needs evidence_id or fact_id")])
        eid = new_id("st", int(timeutil.parse_iso(now).timestamp() * 1000))
        conn.execute("INSERT INTO pb_status_event(event_id,call_id,kind,from_status,to_status,basis,evidence_id,fact_id,occurred_at,"
                     "recorded_at,actor,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (eid, call_id, kind, cur, to_status, basis, evidence_id, fact_id, now, now, actor, reason))
        conn.execute("UPDATE pb_call_edition SET status=?, status_since=?, status_basis=?, dirty_seq=dirty_seq+1 WHERE call_id=?",
                     (to_status, now, basis, call_id))
        return eid
