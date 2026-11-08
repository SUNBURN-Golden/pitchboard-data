"""Offline excerpt replay against a read-only SQLite snapshot; never writes data.

Usage: PYTHONPATH=src python3 scripts/evaluate_simultaneous.py SNAPSHOT BASE_REF
Split by SHA256(outlet_id) parity before tuning: even=tune, odd=heldout.
Partial retained excerpts measure extraction opportunities, not live filled facts.
"""
import hashlib
import json
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

from pbdata.collect.parse import Doc, Segment
from pbdata.profiles import pitchboard as candidate


def policies(module, text):
    return [e.value for e in module.extract(Doc('html', segments=[Segment(0, 'p', text)]))
            if e.field == 'rights.simultaneous']


def main():
    baseline = types.ModuleType('pbdata.profiles.baseline')
    baseline.__package__ = 'pbdata.profiles'
    sys.modules[baseline.__name__] = baseline
    exec(subprocess.check_output(['git', 'show', sys.argv[2] + ':src/pbdata/profiles/pitchboard.py'], text=True), baseline.__dict__)
    conn = sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    gaps = list(conn.execute("SELECT * FROM pb_gap WHERE state='open' AND gap_class='not_extracted' AND predicate='rights.simultaneous' ORDER BY gap_id"))
    result = {}
    for split, parity in [('tune', 0), ('heldout', 1)]:
        group = [g for g in gaps if int(hashlib.sha256(g['subject_id'].encode()).hexdigest(), 16) % 2 == parity]
        before, after, changes = set(), set(), []
        for gap in group:
            rows = conn.execute("SELECT DISTINCT e.excerpt,s.canonical_url FROM pb_source s JOIN pb_raw_ref r ON r.source_id=s.source_id JOIN pb_extraction e ON e.raw_id=r.raw_id WHERE s.owner_outlet_id=? AND r.outcome='ok' AND e.extractor='rules' AND (e.field='rights.simultaneous' OR (e.field='signal.cue' AND json_extract(e.value_json,'$.for')='rights.simultaneous'))", (gap['subject_id'],))
            for row in rows:
                old, new = policies(baseline, row['excerpt']), policies(candidate, row['excerpt'])
                if old:
                    before.add(gap['gap_id'])
                if new:
                    after.add(gap['gap_id'])
                if old != new:
                    changes.append(dict(row, before=old, after=new, gap_id=gap['gap_id']))
        result[split] = dict(gaps=len(group), before=len(before), after=len(after), changes=changes)
    regressions = []
    for row in conn.execute("SELECT DISTINCT excerpt FROM pb_extraction WHERE field='rights.simultaneous' AND extractor='rules'"):
        old, new = policies(baseline, row[0]), policies(candidate, row[0])
        if old != new:
            regressions.append(dict(excerpt=row[0], before=old, after=new))
    result['existing_policy_changes'] = regressions
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
