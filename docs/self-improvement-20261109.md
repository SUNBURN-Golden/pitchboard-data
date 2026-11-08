# Simultaneous policy extraction experiment, 2026-11-09

Decision: do not merge; no held-out improvement.

Baseline: `2dae7897397b5820e6e92f6b7df5d529c8fb9f3f` (`pb-rules-6`).
Candidate: explicit simultaneous-submission declarations may contain a bounded
parenthetical definition. Require the permission immediately after that subject
and definition, so conditional acceptance elsewhere cannot imply permission.
No AI, network collection, relation inference, or database writes are involved.

The live service database was opened with SQLite `mode=ro` and backed up via the
SQLite backup API to `/tmp/pb-self-20261109/snapshot.sqlite3`. All investigation
then used that snapshot with `mode=ro`. The snapshot contained 119 open
`not_extracted` gaps, including 11 for `rights.simultaneous`.

Before viewing policy tuning text, assign outlets by SHA256(outlet_id) parity:
even=tuning, odd=held-out. All snippets from an outlet stay in its partition.
Tune on the Doek parenthetical definition; freeze the candidate and tests before
viewing held-out policy snippets. No changes were made after held-out inspection.

Metric: distinct open simultaneous-policy gaps with a deterministic stated
policy reproduced from retained rules excerpts (extraction opportunities, not
actual promoted facts or live gap resolution).

| Partition | Gaps | Before | After |
| --- | ---: | ---: | ---: |
| Tuning | 5 | 0 (0%) | 1 (20%) |
| Held-out | 6 | 0 (0%) | 0 (0%) |

Held-out has no changed values, hence no new wrong fills. The tuning addition
literally declares simultaneous submissions permitted on Doek's submissions
page. Existing retained `rights.simultaneous` rule excerpts across the complete
snapshot reproduce identical values before and after (zero regressions).
Synthetic tests cover negative policies, conditions, unrelated accepted work,
parenthetical definitions, existing direct declarations, and excerpt reparse.

Reproduce against a read-only snapshot:

```sh
PYTHONPATH=src python3 scripts/evaluate_simultaneous.py SNAPSHOT.sqlite3 2dae7897397b5820e6e92f6b7df5d529c8fb9f3f
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
```

Limitations: raw bodies are intentionally not retained; excerpt replay cannot
measure full-page recall or guarantee promotion through source/label/revision
gates. Do not report the tuning opportunity as a resolved live gap. The held-out
sample is small and has no gain, so the merge condition is not met.

Local tests: the three new tests pass. The full candidate suite runs 142 tests
with three OCR-dependent failures; the baseline has the same three failures
because this environment has no `tesseract` executable. GitHub CI installs the
OCR tools and runs both Python 3.9 and 3.13.

Live crawler, live database, AI settings, and the separate LLM runtime were left
untouched. The backup is an external evaluation artifact, not a data migration.
