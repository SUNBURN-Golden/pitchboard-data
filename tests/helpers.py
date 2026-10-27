import sqlite3

from pbdata import db
from pbdata.ids import new_id, stable_id

NOW = "2026-10-27T00:00:00Z"


def service() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.migrate(conn, "service")
    return conn


def add_source(conn, url="https://example.org/submit", authority="official_primary", permission="allowed_public",
               max_epistemic="verified_fact", source_type="official_guidelines", outlet=None):
    sid = stable_id("src", url)
    conn.execute("INSERT INTO pb_source(source_id,canonical_url,host,source_type,authority,owner_outlet_id,permission,"
                 "permission_basis,max_epistemic) VALUES(?,?,?,?,?,?,?,?,?)",
                 (sid, url, url.split('/')[2], source_type, authority, outlet, permission, "test", max_epistemic))
    return sid


def add_outlet(conn, name="Example Review"):
    oid = new_id("out")
    conn.execute("INSERT INTO pb_outlet VALUES(?,?,?,NULL)", (oid, name, NOW))
    return oid


def add_call(conn, outlet, key="2026-10", scope="book_review", status="candidate"):
    cid = new_id("call")
    conn.execute("INSERT INTO pb_call_edition(call_id,outlet_id,edition_key,scope_ref,status,created_at) VALUES(?,?,?,?,?,?)",
                 (cid, outlet, key, scope, status, NOW))
    return cid


def add_revision(conn, source_id, content_hash="h1", fetched_at=NOW):
    rid = new_id("rev")
    conn.execute("INSERT INTO pb_source_revision(revision_id,source_id,content_hash,change_state,fetched_at) VALUES(?,?,?,?,?)",
                 (rid, source_id, content_hash, "new", fetched_at))
    return rid
