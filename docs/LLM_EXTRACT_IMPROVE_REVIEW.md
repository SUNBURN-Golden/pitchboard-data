# DeepSeek shadow improvement — BUILD REVIEW

Review date: 2026-11-10

## Scope and method

- Branch: `feat/deepseek-shadow-target-gaps` at `e47c0eb` (`feat(shadow): score
  targeted evidence and ledger ceilings offline`).
- Base: `origin/main` at `63baad7`. The merge base is `63baad7`, so the whole
  feature is new relative to main.
- Diff: 26 files, +5,824 lines, no modifications to existing collector,
  `collect/net.py`, parser, profile or daemon paths. All new runtime code lives
  under `src/pbdata/collect/shadow/`, with two CLIs under `scripts/`.
- Method: commit-by-commit diff read, line-level inspection of the planner,
  runner, source cache, schema, review, digest, state and report code, an
  independent test run, an offline held-out scoring run, and targeted ad-hoc
  probes for the evidence and hash contracts.
- Constraints checked against the brief: checked-in config is disabled, no live
  DB/crawler/cron/Instagram/HANSET path is touched, no credential is written or
  printed, and SOULBOUND/Instagram are denied by policy and planner.

## Independent verification

Commands run from `/workspace/pitchboard-data-improve`.

| Check | Result |
| --- | --- |
| `PYTHONPATH=src python3 -B -m pytest -q tests/test_llm_extract.py tests/test_llm_compare.py tests/test_llm_improve.py tests/test_fee_submission.py` | `84 passed, 189 subtests passed` |
| `PYTHONPATH=src pytest -q` | `220 passed, 221 subtests passed, 3 failed` |
| Same 3 failures on a detached worktree at `origin/main` | Reproduced: `test_image_ocr`, `test_scanned_pdf_uses_ocr`, `E2E::test_full_flow`; environmental OCR (tesseract absent), unrelated to this branch |
| Offline held-out score (`--input-mode shadow --operation score`) | Completed offline, `eligible_pages: 188`, `scored_predictions: 0`; `/workspace/pb-eval` tree hash unchanged before/after |
| Provider/socket/key access in score mode | None: no config `enabled` check, no `load_key`, no `PolicyFetcher` |
| Checked-in `config/llm_extract.json` | `enabled:false`; no branch code sets it |

The guard set is intact: reservations are permanent, caps and the time window
fail closed, Stop reasons are enumerated, the batch stops on the same terminal
outcomes as the single-URL CLI, and no code writes to the service DB.

## Findings

Severity is relative to the stated goals and the intended 40-URL activation. The
shadow remains disabled, so none of these is a live production incident today.

### F1 — High (release blocker for Stages 2, 3 and 5 on Korean pages): the service `content_hash` is not the shadow `body_sha256`

The collector stores a hash of the **UTF-8 re-encoding of the latin-1-decoded
body**:

- `src/pbdata/collect/net.py:241` — `res.content_hash = sha256_hex(data.decode("latin-1")) if res.body else ""`
- `src/pbdata/ids.py:39-40` — `sha256_hex(text) = sha256(text.encode("utf-8"))`

The shadow uses the **raw body bytes**:

- `src/pbdata/collect/shadow/policy.py` — `Page.body_sha256 = digest(self.body)` (`digest` = raw `hashlib.sha256`)
- `src/pbdata/collect/shadow/planner.py:144` — validates the exported `content_hash` but does not translate it

For ASCII bodies the two values agree. For any non-ASCII body — i.e. essentially
every Korean Pitchboard page — they differ. Verified empirically: ASCII matched;
a body containing `가` produced two different hashes (the latin-1 round trip is
not byte-preserving for multi-byte UTF-8).

The design explicitly specifies the service value as the key:
`docs/LLM_EXTRACT_IMPROVE_DESIGN.md:244` ("Use `pb_raw_ref.content_hash` as the
known body hash") and `:254` (flag `source_changed` when the body changed).

Affected paths, all fed the service hash:

1. **Source-cache reuse** — `src/pbdata/collect/shadow/runner.py:106` calls
   `source_cache.load(..., known_hash, ...)`; `source_cache.cache_key`
   (`source_cache.py:28-29`) hashes that value. A cache entry saved under
   `page.body_sha256` (`source_cache.py:80`) is therefore never found for a
   planner target. Demonstrated: save under raw hash, `load(..., service_hash)`
   returns `None`; a batch target then calls the fetcher (`shadow_error` in the
   probe) instead of reusing. This defeats the bounded-copy reuse and
   no-re-fetch requirement of Stage 2 for Korean pages.
2. **`source_changed`** — `runner.py:147` compares the service hash with the raw
   hash, so it is permanently `True` for Korean targets. Consequences:
   - `report.py:339` excludes those fields from `accepted_targeted`, and
     `report.py:343` excludes candidate fills from coverage;
   - `digest.py:9` suppresses all digest drafts;
   - `review.py:90-91` tags every successful Korean row with `source_changed`.
3. **Planner history / static skip** — `planner.py:152-160` compares persisted
   `body_sha256` with the exported `content_hash`. `shadow_attempted` and `done`
   therefore never become true for Korean pages, so `skip_static_already_done`
   and the time-sensitive boundary retry never trigger. The batch will re-fetch
   and re-pay for the same static success every day until the shared cap stops
   it. This directly undercuts the gap-batch cost objective.

Why the tests did not catch it: `tests/test_llm_improve.py:22` builds candidates
with `content_hash=common.digest(page().body)`, which is the raw-byte hash, and
`tests/test_llm_improve.py:60,376` reuse that value in history. No test builds a
service-style hash from a non-ASCII body, and there is no `source_changed`
assertion.

Recommended fix (shadow-only; do not change the live collector):

- Derive a `source_content_hash` from the fetched body using the service
  contract (`sha256_hex(body.decode("latin-1"))`, or an equivalent local helper)
  and use it for target matching, cache lookup/save and planner history.
- Persist both `body_sha256` (raw identity, held-out compatibility) and
  `source_content_hash` in the attempt record and cache envelope; bump the cache
  format. Have `report.py` look up target/history rows by
  `source_content_hash`.
- Add a regression test with a Korean body and a service-style
  `content_hash`: assert cache hit with zero fetch, `source_changed=False`,
  static `done` skip on the next plan, targeted coverage counted, and digest
  drafts emitted. Also add a test that a genuinely changed body still sets
  `source_changed=True`.

### F2 — Medium: `source_changed` and cache-reuse semantics lack contract-level tests

Even without F1, the cross-component contract (planner export → cache key →
record → planner history → report) is only tested with one synthetic hash
convention. The missing tests are exactly the ones that would have caught F1 and
would catch a future collector-side change. Treat the F1 regression tests as
part of the fix, not optional coverage.

### F3 — Low: plan-only candidate counts ignore prior history

`scripts/llm_extract_shadow_batch.py:31-39` sets `history = []` unless the batch
will execute, and plan-only never reads an existing ledger. The operator-facing
counts in the design's step 2 therefore include static pages already completed
and time-sensitive pages with no nearby boundary; execution will then select
fewer targets. This is not a safety issue (execution re-plans with history), but
it makes the "inspect counts" gate less informative. Either document the upper
bound explicitly or allow a read-only ledger load in plan-only.

### F4 — Low: the cache key omits the final URL and text hash

`source_cache.py:28-29` keys on `(FORMAT, POLICY, PARSER_VERSION, requested URL,
body_hash)` while the design (`LLM_EXTRACT_IMPROVE_DESIGN.md:218-222`) expects
the final URL and text hash to be part of the identity. If the same requested
URL and identical body resolve to a different final URL, `save` finds the
existing entry and raises `corrupt_source_cache` (`source_cache.py:83-88`)
instead of storing a distinct snapshot. Fail-closed, but it can convert a benign
redirect change into a lost page. Include `final_url`/`text_sha256` in the key,
or treat the mismatch as a new entry.

### F5 — Low: gap input readers do not require a regular file

`planner.read_json` (`planner.py:19-22`) and `read_db` (`planner.py:36-56`) use
`no_symlinks` and size/snapshot checks, but not `stat.S_ISREG` (and no
`O_NONBLOCK`). A FIFO or device path can block a run. The DB path is already
documented as a metadata-checked raw snapshot rather than a true SQLite
consistent read; requiring a regular file (and rejecting sidecars) would make
the operator contract tighter. Low risk because the operator supplies the path.

### F6 — Low: `sqlite3.Connection.deserialize` requires Python ≥ 3.11

`planner.py:59` and `report.py:122` use `deserialize`, while
`pyproject.toml:5` declares `requires-python = ">=3.9"`. The design notes the
3.11+ requirement, but the package metadata still advertises 3.9. Either gate the
code with a clear error on older interpreters or raise the declared floor.

### F7 — Low: the activation runbook still shows the old hardcoded-URL cron

The operator handoff for the 40-URL batch is present in
`docs/LLM_EXTRACT_IMPROVE_DESIGN.md:440-470,679`, satisfying the brief. However,
`docs/LLM_EXTRACT_ACTIVATE.md:139` still says "Three pages a day" and
`:148` still contains a cron line invoking `scripts/llm_extract_shadow.py` with
hardcoded URLs. An operator following the older runbook could miss the batch
entirely. Add a prominent cross-reference and mark the old example as superseded
(examples only; do not edit any crontab).

### F8 — Low (residual lexical risk): a year can satisfy a fee amount

Amount support is lexical over numeric tokens (`schema.py:221-227`). A quote
containing a fee cue and a four-digit year accepts that year as the amount:
verified with quote `"Entry fee. Submissions close December 2026."` and
`amount_min=amount_max="2026"` — accepted, no reason. This is shadow-only and
the quote must still be verbatim, so it is not an unsafe promotion, but it can
manufacture a fee value. Excluding date spans (or requiring the amount token to
be near the fee cue, and not immediately preceded by a month/day) would reduce
it. Also note that `find_dates`-derived years and plain amounts share the same
token space.

## Stage conformance summary

1. **Gap-targeted batching** — Implemented and well covered: priority order,
   fee-first tiers, SOULBOUND/Instagram exclusion, per-gap best source, outlet
   round-robin, URL dedup, `--limit` default 40 ≤ cap 50, shared fetcher/ledger,
   terminal stop, no service-DB writes. Caveat: F1 disables history-based static
   skipping for Korean pages, so the cost benefit is not realized there.
2. **Bounded raw source text** — Implemented safely (bounded parsed text only,
   0600 atomic files, 30-day expiry, integrity checks, no HTML). Reuse and
   `source_changed` are broken for Korean targets by F1.
3. **Stricter accept, conflicts, second opinions** — Implemented: exact
   segment-local quotes, token support for amounts/dates/currency/units/negation,
   fee-specific states, contact rejection, review queue, call disagreement
   second opinions, idempotent recovery. Residual lexical risk F8.
4. **Digest drafts** — Deterministic ko/en one-liners, written only under the
   private shadow root, suppressed on non-ok/source-changed/conflict. Works as
   specified, but F1 suppresses Korean drafts in practice.
5. **Rules vs DeepSeek comparison** — Offline default is provider-free, scoring
   is frozen-cohort based, ledger ceilings and discrepancies are reported, and
   the held-out tree is unchanged. Python floor (F6) and plan-only counts (F3)
   are the only caveats.

## Verdict

The safety and isolation requirements are met: config stays disabled, the live
service DB/crawler/cron are untouched, credentials are never printed, and the
offline scorer makes no provider or network calls. The implementation is
otherwise solid and covered by 84 focused tests.

However, **F1 is a genuine integration defect** between the live collector's
`content_hash` contract and the shadow's raw-byte hash assumption. Because the
target corpus is predominantly Korean, enabling the 40-URL batch as-is would
re-fetch pages the bounded cache was meant to reuse, mark every Korean success
as `source_changed`, suppress targeted coverage and digest drafts, and lose the
static-page skip that protects the daily budget. It should be fixed, with the
contract-level regression tests from F1/F2, before raising the isolated cron
from the hardcoded URLs to the gap-targeted batch. F3–F8 can be handled
incrementally.

## REVIEW FIX log (2026-11-10)

All findings were addressed on `feat/deepseek-shadow-target-gaps`; no live
DB/crawler/cron/private config was touched and `config/llm_extract.json` remains
`enabled=false`.

| Finding | Resolution |
| --- | --- |
| F1 | Added `common.source_content_hash` (latin-1 decode then UTF-8 SHA-256) and `Page.source_content_hash`. The attempt record now carries both `body_sha256` and `source_content_hash`; cache lookup/save, planner history and `source_changed` use the service contract. Cache format bumped to `pb-shadow-text-2`. |
| F2 | Added Korean-body contract tests: service hash differs from raw hash, cache hit with zero fetch, `source_changed=False`, static skip on the next plan, targeted coverage scored, ko/en digest drafts emitted, and a genuinely changed body still flags `source_changed=True`. |
| F3 | Plan-only reads an initialized ledger through the new read-only `state.read_history` snapshot, so counts reflect prior static completions. It creates no lock, identity, directory or write. |
| F4 | The snapshot filename now adds final URL + parsed-text-hash identity; differing redirect targets are distinct snapshots. Lookup can filter by `final_url`/`text_sha256`, and a regression test saves two final URLs for one body. |
| F5 | `planner.read_json`/`read_db` open with `O_NOFOLLOW|O_NONBLOCK` and require `stat.S_ISREG`; FIFOs, devices and directories fail with `unsafe_file` instead of blocking. |
| F6 | `requires-python` raised to `>=3.11`, with defensive `python_requires_311` stops before `Connection.deserialize` in the planner, report and read-history paths. |
| F7 | `LLM_EXTRACT_ACTIVATE.md` now marks the three-page/hardcoded-URL example as superseded and cross-references the 40-URL batch handoff. No cron is edited. |
| F8 | Fee amount extraction excludes spans already parsed as dates and month+year spans such as `December 2026`, so a year cannot satisfy an amount. A separate amount in the same quote still validates. |

Verification (`PYTHONPATH=src python3 -B -m pytest -q -p no:cacheprovider`):
the four affected suites pass **91 tests / 189 subtests**; the full run passes
**227 tests / 221 subtests** with the same three pre-existing OCR-dependent
failures (`test_image_ocr`, `test_scanned_pdf_uses_ocr`, `E2E.test_full_flow`)
caused by a missing `tesseract`.
