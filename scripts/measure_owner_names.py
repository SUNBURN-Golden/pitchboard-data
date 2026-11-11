#!/usr/bin/env python3
"""Measure junk owner/outlet names in a service DB. The DB is opened strictly read-only (mode=ro).

"Before" junk verdicts use an INDEPENDENT simple checker (CTA keyword substrings + ^Q\\d+$ + empty),
deliberately not the production rule. "After" names are what the new naming code would display,
computed by the real read-only code paths (pbdata.daemon.project.build_payload / _display_name).
Raw page bodies are not stored in the DB, so the DB-only simulation falls back to the host-derived
name; at registration time the same rules prefer og:site_name / <title> brand when a page is fetched.

Usage: python3 scripts/measure_owner_names.py [DB_PATH]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pbdata.collect import names                      # production rule (the "after" producer)
from pbdata.daemon import project

DEFAULT_DB = "/workspace/pitchboard/data-root/service_db/pitchboard_kg.sqlite3"

# --- independent checker: intentionally simple and separate from pbdata.collect.names ---
CTA_KEYWORDS = ("접수하기", "신청하기", "바로가기", "더보기", "apply now", "apply online", "submit",
                "click here", "read more", "learn more", "sign up")


def independent_junk(name) -> bool:
    s = (name or "").strip()
    if not s:
        return True
    if re.fullmatch(r"Q\d+", s):
        return True
    return any(k in s.lower() for k in CTA_KEYWORDS)


# --- held-out sets (NOT used while tuning; tuning saw only the two live junk owners) ---
HELDOUT_GOOD = [   # real owner/outlet names currently in the DB, Korean and English
    "금성출판사", "한국문학번역원 공지", "한국문화예술위원회 공지/공모", "서울문화재단 공지/공모",
    "문장웹진(한국문화예술위원회)", "글여울",
    "Narrative Magazine", "New England Review", "Geist", "Douglas College", "Vassar College",
    "Gimm-Young Publishers", "Writer's Digest Competitions", "Korean Children's Book Association",
    "Going Down Swinging", "Rue Morgue", "Taddle Creek", "Plumwood Mountain",
]
HELDOUT_JUNK = [
    "지금 신청", "Learn more", "Q4115189", "홈페이지 바로가기", "Submit here",
    "더 보기", "Apply online", "Q27948", "Click here to apply", "지원하기",
]


def main() -> int:
    db_path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB).resolve()
    db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    # --- live calls: displayed owner name before vs after (real projection code, read-only) ---
    calls = []
    for r in db.execute("SELECT call_id, payload FROM pb_search_projection ORDER BY call_id"):
        p = json.loads(r["payload"])
        before = p["owner"]["name"]
        after = project.build_payload(db, r["call_id"])["owner"]["name"]
        site = db.execute("SELECT ss.origin FROM pb_call_edition c JOIN pb_source_site ss ON ss.outlet_id=c.outlet_id "
                          "WHERE c.call_id=? LIMIT 1", (r["call_id"],)).fetchone()
        calls.append({"call_id": r["call_id"], "site": site["origin"] if site else "", "before": before, "after": after})
    call_junk_before = [c for c in calls if independent_junk(c["before"])]
    call_junk_after = [c for c in calls if independent_junk(c["after"])]

    # --- live outlets: canonical_name before vs after (real display guard, read-only) ---
    outlets = []
    for r in db.execute("SELECT outlet_id, canonical_name FROM pb_outlet ORDER BY outlet_id"):
        after = project._display_name(db, r["outlet_id"], r["canonical_name"])
        outlets.append({"outlet_id": r["outlet_id"], "before": r["canonical_name"], "after": after})
    outlet_junk_before = [o for o in outlets if independent_junk(o["before"])]
    outlet_junk_after = [o for o in outlets if independent_junk(o["after"])]

    print("== live DB: %s (read-only) ==" % db_path)
    print("calls: %d, outlets: %d" % (len(calls), len(outlets)))
    print("\n-- junk displayed owner names, before -> after-simulated --")
    for c in call_junk_before:
        print("  %s  %s\n    %r -> %r" % (c["call_id"], c["site"], c["before"], c["after"]))
    for o in outlet_junk_before:
        print("  %s  %r -> %r" % (o["outlet_id"], o["before"], o["after"]))

    # --- held-out good names: must exist in the DB, be fine today, and stay unchanged ---
    good_changed, good_missing = [], []
    for g in HELDOUT_GOOD:
        row = db.execute("SELECT outlet_id, canonical_name FROM pb_outlet WHERE canonical_name=?", (g,)).fetchone()
        if row is None:
            good_missing.append(g)
            continue
        assert not independent_junk(g), "held-out good name judged junk by independent checker: %r" % g
        after = project._display_name(db, row["outlet_id"], row["canonical_name"])
        if after != g or names.pick_name([g], "") != g:
            good_changed.append((g, after))

    # --- held-out junk: must be caught by the production rule (and by the independent checker) ---
    junk_caught = [s for s in HELDOUT_JUNK if names.is_junk_name(s)]
    junk_caught_indep = [s for s in HELDOUT_JUNK if independent_junk(s)]

    print("\n-- held-out good names (real, currently fine): %d --" % len(HELDOUT_GOOD))
    print("  wrongly changed: %d%s" % (len(good_changed), (" " + repr(good_changed)) if good_changed else ""))
    if good_missing:
        print("  WARNING: not found in DB: %r" % good_missing)
    print("-- held-out junk (not used in tuning): %d --" % len(HELDOUT_JUNK))
    print("  caught by new rule: %d/%d %s" % (len(junk_caught), len(HELDOUT_JUNK),
                                             [s for s in HELDOUT_JUNK if s not in junk_caught] or ""))
    print("  caught by independent checker: %d/%d" % (len(junk_caught_indep), len(HELDOUT_JUNK)))

    print("\n== summary ==")
    print("live calls  junk shown: %d -> %d (after-simulated)" % (len(call_junk_before), len(call_junk_after)))
    print("live outlets junk shown: %d -> %d (after-simulated)" % (len(outlet_junk_before), len(outlet_junk_after)))
    print("held-out good names wrongly changed: %d/%d" % (len(good_changed), len(HELDOUT_GOOD)))
    print("held-out junk caught: %d/%d" % (len(junk_caught), len(HELDOUT_JUNK)))
    ok = not call_junk_after and not outlet_junk_after and not good_changed and not good_missing \
        and len(junk_caught) == len(HELDOUT_JUNK)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
