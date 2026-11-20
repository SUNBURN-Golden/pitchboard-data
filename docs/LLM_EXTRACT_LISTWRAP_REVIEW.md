# Build review: singleton list wrapper unwrap

> This review records the November 18 implementation. See the [2026-11-20 cap changes](../README.md#caps-removed-2026-11-20) for current defaults.

Stage: **BUILD REVIEW**, 2026-11-18. Branch `fix/shadow-qwen-list-unwrap`,
commit `e2f403a` ("fix(shadow): unwrap singleton object lists before schema
validation") vs `origin/main`. Scope of the diff: `src/pbdata/collect/shadow/schema.py`
(+14/-3 lines), `tests/test_llm_extract.py`, `tests/test_qwen_fallback.py`, and
`docs/LLM_EXTRACT_LISTWRAP_DESIGN.md`. No site files, runtime configuration,
live DB, or crawler code touched.

## Test execution

- `python3 -m pytest -q` from the repo root initially failed collection with
  `ModuleNotFoundError: No module named 'pbdata'` — the src-layout package is
  not installed in this environment. This is an environment setup issue, not a
  code defect: the README and CI (`.github/workflows/test.yml`) both run tests
  with `PYTHONPATH=src:tests`.
- `PYTHONPATH=src:tests python3 -m pytest -q`: **329 passed, 391 subtests
  passed** (11.6 s), full suite, nothing skipped.
- CI-parity cross-check `PYTHONPATH=src:tests python3 -m unittest discover -s tests`:
  **OK**.
- No-network constraint: new runner/fallback shape cases patch
  `socket.socket.connect` to raise; all transports are injected with synthetic
  fixtures and fake keys. `tests/test_egress.py` was spot-checked and binds
  only `127.0.0.1` servers, normalizes `localhost` proxies to loopback, and
  policy-rejects non-loopback proxy addresses (e.g. `socks5h://8.8.8.8:1080`).
  The full suite makes no external network calls.

## Correctness vs the stated goal

1. **Unwrap rule is exact.** `response()` (schema.py:333-335) unwraps only when
   `isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)`,
   one level, by rebinding (no input mutation — tests assert the raw response
   is byte-identical before/after). The inner dict goes to the unchanged
   `validate()`. Both providers share this path; direct `validate([dict])`
   still raises `invalid_schema` (explicitly tested).
2. **Rejected lists fail closed with safe metadata.** `_shape_summary()`
   (schema.py:100-102) returns immediately for any list with exactly
   `{"top_type": "list", "list_len": len(raw)}` — no element inspection, no
   keys, values, quotes, or snippets. Covers `[]`, >=2 elements, and singleton
   non-dict (string, int, float, bool, null, nested list), all tested with
   sentinel-leak assertions and an `int` type check on `list_len`.
3. **`unwrapped_list` flag placement is correct.** `_shape_summary()` clears
   its output at entry, so the flag is written in a `finally` after
   `validate()` (schema.py:336-340): it survives both success and
   `Stop("invalid_schema")` from the inner dict, never suppresses the
   exception, is a literal `True` (never `list_len`), and is absent when no
   unwrap occurred. Entry clearing in `response()` plus the runner's reuse of
   the same `schema_shape` dict across primary/fallback is covered by
   `test_unwrap_flag_clears_on_later_response`.
4. **No validation relaxed.** The diff touches only `_shape_summary()`
   diagnostics and `response()` normalization. `validate()` acceptance logic,
   verbatim-evidence/support checks, contact-detail regexes, segment-local
   quote matching, temporal checks, the `FIELDS` key set, missing/extra-key
   handling, model allowlists, usage invariants, `finish_reason`/message
   guards, and strict `loads()` (duplicate-key and NaN rejection at all
   depths) are byte-identical to `origin/main`. Object/wrapped-object parity
   tests assert identical fields, reasons, and usage, including a new
   non-verbatim-value case proving evidence checks still fire through the
   unwrap.
5. **Guards precede unwrap.** `test_wrapped_content_does_not_bypass_response_guards`
   patches `validate` to fail if reached and confirms model/usage/finish/
   refusal/tool_calls/reasoning guards still fire first with empty shape.
   Credential guards run in the runner on `dumps(raw)` before `response()`;
   wrapped-credential tests (primary and Qwen fallback) confirm
   `credential_in_response` with empty shape and no secret in any artifact.

## Judgment calls reviewed

6. **`SCHEMA_HASH` bump (`shape_version` -> `missing-null-listwrap-2`).** Not
   required by the goal text but justified: acceptance semantics changed, and
   provenance must distinguish them. Independently verified the three
   consumers the design doc names: runner dedup identity (runner.py:152),
   report exclusion `schema_prompt_mismatch` (report.py:275), and planner
   done-matching (planner.py:194). Consequence: old-hash rows are re-planned/
   re-reserved if extraction is ever activated, and excluded from reports
   until re-run. This is documented as intended, is safe while the change
   stays offline (AI extraction off, cost cap 0), and is regression-tested by
   pinning the exact pre-change digest and asserting the new hash differs.
   `VERSION` and `evidence_version` unchanged.
7. **Prompt strengthening deferred — compliant.** The goal allowed it "only
   if trivial and safe". `PROMPT` already says "Return only a JSON object"
   (schema.py:20) and request bodies already set
   `response_format={"type":"json_object"}`; any wording change would alter
   `PROMPT_HASH`, which is pinned to an exact digest in an existing test and
   feeds the runner dedup identity. Deferral is the correct call; `PROMPT`,
   `PROMPT_HASH`, and request bodies are unchanged in the diff.
8. **Two-key list summary drops `missing`/`extra`/count keys.** Verified by
   grep across `src/`: no production code reads individual `schema_shape`
   keys (the dict is only written by schema.py and persisted by runner.py),
   so no code path breaks. Only offline analysis of persisted rows must
   tolerate the new shape, as the design doc states.

## Minor observations (non-blocking)

9. The type map in `_shape_summary()` still lists `list: "list"`
   (schema.py:103); it is now unreachable for lists because the `isinstance`
   branch returns first. Harmless dead entry; no change requested.
10. The bare `python3 -m pytest -q` invocation only works once `pbdata` is
    importable (editable install or `PYTHONPATH=src:tests`, per README/CI).
    Worth keeping in mind for future stage instructions; not a defect in
    this commit.

## Findings

1. No correctness defects found: the unwrap rule, rejected-list metadata,
   flag lifecycle, and guard ordering match the goal exactly, and no
   validation was relaxed.
2. No secret/content leakage paths found: rejected-list summaries are
   exactly two safe keys, credential guards precede unwrap on both
   providers, and sentinel assertions cover persisted artifacts.
3. Full suite passes under both pytest (329 passed, 391 subtests) and the
   CI unittest command, with no external network access; the initial bare
   pytest collection error is an environment path issue, resolved with the
   documented `PYTHONPATH=src:tests`.
4. The `SCHEMA_HASH` bump and prompt deferral are deliberate, documented,
   and tested decisions consistent with the goal's constraints; the
   re-plan/re-charge consequences are acceptable while extraction remains
   offline.
5. Non-blocking cosmetics only: unreachable `list` entry in the type map,
   and the environment note about the bare pytest invocation.

VERDICT: APPROVE

## Re-review

Stage: **RE-REVIEW**, 2026-11-18. Full diff vs `origin/main` re-examined
(`schema.py` +14/-3, `tests/test_llm_extract.py`, `tests/test_qwen_fallback.py`,
two docs; no site files, runtime config, live DB, or crawler changes).

- `PYTHONPATH=src:tests python3 -m pytest -q` from the repo root:
  **329 passed, 391 subtests passed** (12.7 s), nothing skipped, no external
  network (new runner/fallback cases patch `socket.socket.connect`).
- Unwrap rule confirmed exact: `response()` (schema.py:333-335) unwraps only a
  one-element list whose element is a dict, one level, by rebinding; the inner
  dict reaches the unchanged `validate()`. Direct `validate()` still rejects
  every list, including `[dict]` (tested).
- Rejected lists (`[]`, `>=2` elements, singleton non-dict) produce exactly
  `{"top_type":"list","list_len":N}`; `_shape_summary()` returns before any
  element inspection, so no keys, values, or quotes can leak. `list_len` is
  asserted `int`.
- `unwrapped_list` flag: set only on actual unwrap, in a `finally` after
  `validate()`, literal `True`, absent otherwise, cleared by response-entry
  `schema_shape.clear()` on reuse — all tested.
- No validation relaxed: `validate()`, verbatim-evidence/support checks,
  `FIELDS`, missing/extra-key handling, model allowlists, usage invariants,
  finish/message guards, and strict `loads()` are unchanged; guard tests patch
  `validate` to prove wrapped content cannot bypass them, and credential guards
  still precede shape logging on both providers.
- `SCHEMA_HASH` bump (`missing-null-listwrap-2`) and prompt deferral remain
  correct, documented, and pinned by regression tests; `PROMPT`/`PROMPT_HASH`
  and request bodies unchanged.
- Prior non-blocking notes stand (unreachable `list` entry in the type map;
  bare `pytest` needs `pbdata` importable per README/CI). No new findings.

FINAL: APPROVE
