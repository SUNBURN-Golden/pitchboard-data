# Shadow extraction: singleton list wrapper

Stage: **BUILD**, 2026-11-18. Implementation and offline regression tests complete.
The design-stage decisions and review below are retained as implementation context.
Target: `src/pbdata/collect/shadow/schema.py` on
`fix/shadow-qwen-list-unwrap`.

## Problem

User-reported evidence from the 2026-11-17 KST shadow run (runtime `2acde7a`):
four of six Qwen fallback attempts ended as `invalid_schema`, with
`schema_shape.top_type="list"`, `missing=[]`, and `extra=[]`. This identifies
array responses but does not establish their lengths or element types. Currently,
`response()` decodes content and passes it directly to `validate()`, which rejects
every non-dictionary. A singleton object wrapper can be normalized safely; other
arrays must remain rejected.

## Exact behavior

Immediately after successful `loads()` in `response()`, unwrap content only when
it is a list of exactly one element and that element is a dictionary. Unwrap one
level only, without mutating the input. Pass that dictionary to the existing
`validate()` unchanged. Apply this shared response behavior to both providers;
direct calls to `validate()` still reject every list.

| Decoded content | Result | `schema_shape` |
| --- | --- | --- |
| Ordinary dictionary | Existing validation | Existing summary, unchanged |
| `[dictionary]` with exactly the expected keys | Existing validation of dictionary | `{"unwrapped_list":true}` |
| `[dictionary]` with missing or extra keys | Existing validation of dictionary | Existing dictionary key summary plus `unwrapped_list=true` |
| Empty list, list of two or more elements, or singleton non-dictionary | `invalid_schema` | Exactly `{"top_type":"list","list_len":N}` |
| Other non-dictionary | Existing rejection | Existing summary, unchanged |

For rejected arrays, `N` is an integer from `len(content)`. `_shape_summary()`
must return after recording these two fields, without inspecting elements or
including element keys, types, values, quotes, or snippets. This list summary
also applies when a list is rejected by a direct `validate()` call.

For unwrapped content, dictionary diagnostics describe the inner object;
`top_type`, when present, is `dict`. Do not add `list_len` or a false unwrap flag.
Because `_shape_summary()` clears its output, add the true flag in a `finally`
around validation, only when unwrapping occurred and `schema_shape` was supplied.
Preserve it on both success and `Stop("invalid_schema")`, without suppressing the
exception. Keep response-entry clearing so reused metadata cannot leak into a
later response, including failures before content validation.

Keep `FIELDS` exactly as it is: `open_call`, `deadline`, `pay`, `eligibility`, and
`submission_fee`. Preserve existing missing-key normalization to null with
`missing_key` reasons and rejection of extra keys. Preserve all value, verbatim
evidence, contact-detail, segment, and temporal checks and field rejection reasons.
Model allowlists, usage invariants, finish reason, message checks, strict JSON
parsing, credential guards, fallback policy, and accounting remain unchanged.

Update the schema hash's `shape_version` from `missing-null-1` to
`missing-null-listwrap-2` when implementing, so provenance distinguishes this
acceptance change. Keep `VERSION="pb-shadow-2"` and the evidence version unchanged.
Defer the optional prompt strengthening: the shared prompt already says "Return
only a JSON object" and request bodies already set `response_format` to
`json_object`. Any wording change would alter `PROMPT_HASH`, which is pinned to
an exact digest in `test_hash_distinguishes_shape_behavior_without_changing_prompt`
and is part of the runner dedup identity, so it is not trivially safe. Keep
`PROMPT`, `PROMPT_HASH`, and request bodies unchanged for this fix.

## Test plan

- Extend `tests/test_llm_extract.py` with paired object/wrapped-object cases:
  valid evidence, all-null fields, missing keys (including `[{}]`), extra keys,
  and invalid evidence. Fields, reasons, and usage must match the ordinary object;
  only unwrap metadata differs. Verify direct `validate([object])` still rejects.
- Reject `[]`, two valid objects, mixed multi-element arrays, and singleton
  string, number, boolean, null, or nested array. Assert exact two-key summaries
  and integer lengths; synthetic private-value sentinels must never enter them.
  Only the list branch of `_shape_summary()` changes; every other non-dictionary
  top type keeps its existing five-key summary.
- Verify unwrap metadata survives extra-key rejection, clears on subsequent
  responses, works with `schema_shape=None`, and never alters inputs. Exercise
  wrapped content with bad model, usage, finish reason, refusal, tool calls, or
  reasoning content; existing failure codes and empty prevalidation metadata stay.
- Extend shared runner cases and `tests/test_qwen_fallback.py` to check primary
  and fallback results, persisted JSONL/temporary ledger metadata, credential
  exclusion, and unchanged call/cost accounting. Update the two existing sites
  that assert the legacy five-key summary for `[]`: `shape_cases()` and
  `test_extra_wrapper_and_non_dict_shapes_fail_closed` in
  `tests/test_llm_extract.py`. Add a hash regression that pins the actual
  pre-change `SCHEMA_HASH` (the digest with `shape_version="missing-null-1"`)
  and asserts the new hash differs from it; the existing older-baseline hash
  test stays as is.
- Use only synthetic fixtures, injected transports, fake keys, and temporary
  state for new tests; block network access. Implementation acceptance requires
  `python3 -m pytest -q` from the repo root, with all tests passing.

No tests were run in the original documentation-only stage. The existing full suite has
loopback HTTP/SOCKS tests in `tests/test_egress.py`. Verified 2026-11-18: those
tests bind only `127.0.0.1` servers, remap the one public-looking destination
(`example.org:80`) to the local HTTP server, reject non-loopback proxy addresses
by policy before connecting, and one test asserts every connection attempt is
loopback. The suite therefore makes no external network calls; the no-network
constraint is satisfied by running it in full, not by skipping egress tests.
Do not silently skip tests or claim a full pass from a subset.

## Risks and boundaries

The observed failures may include arrays that remain ineligible; no recovery
rate is promised. Metadata consumers must accept the two-key rejected-list
summary and the unwrap flag even on successful responses. The flag records
normalization, not evidence validity. Verified 2026-11-18: no production code
reads individual `schema_shape` keys (the dict is only persisted), so dropping
`missing`/`extra`/count keys from rejected-list summaries breaks no code path;
only offline analysis of persisted rows must tolerate the new shape.

The `shape_version` bump changes `SCHEMA_HASH`, with three verified downstream
effects once the new code runs: the runner dedup key includes `SCHEMA_HASH`
(`runner.py`), so previously attempted page/as_of/config combinations become new
identities and would be re-reserved and re-charged if extraction were activated;
the report excludes old-hash rows as `schema_prompt_mismatch` (`report.py`); and
the planner no longer counts old-hash successful rows as done (`planner.py`),
so those pages become eligible for re-planning. These are the intended
consequences of distinguishing the acceptance change in provenance, not bugs to
work around. Do not replay attempts or activate extraction here; the bump is
safe precisely because this change stays offline.

The design stage added only this document. Do not access the live DB, run the live
crawler, change runtime configuration or site files, expose secrets, push, or
commit to main. Build implementation remains confined to schema handling and
its offline regression tests.

## Review

2026-11-18, design-review stage. Verdict: **APPROVE**, with the corrections
below applied directly to this document. The core design is safe as specified:

- No validation weakening. Unwrapping is confined to `response()`, applies only
  to a one-element list whose element is a dict, unwraps exactly one level, and
  passes the inner dict to the unchanged `validate()`. Verbatim-evidence,
  contact-detail, segment, temporal, FIELDS key-set, missing/extra-key,
  usage/model/finish_reason, and strict-JSON checks are untouched. Direct
  `validate()` calls still reject every list, including `[dict]`.
- No content leakage. Rejected arrays get exactly
  `{"top_type":"list","list_len":N}` with `N=len(content)`; the list branch of
  `_shape_summary()` returns before inspecting elements. Unwrapped-dict
  summaries keep the existing names-only, bounded, redacted-count key
  diagnostics; no values, quotes, or snippets are added anywhere. The
  `unwrapped_list` flag is a literal boolean, never `list_len`, and is absent
  when no unwrap occurred.
- The `finally` flag placement is correct against the actual code:
  `_shape_summary()` clears its output at entry, so setting the flag after
  `validate()` returns or raises preserves it on both success and
  `Stop("invalid_schema")` without suppressing the exception; `response()`'s
  entry clearing still wipes stale metadata on prevalidation failures and on
  the runner's fallback reuse of the same dict.
- Unwrapping cannot bypass the credential guard: the runner checks
  `key in dumps(raw)` before calling `response()`, and the credential test
  patches `validate` to prove shape logging never runs for poisoned responses.
- Applying the unwrap to both providers in shared `response()` code is
  intentional and equally bounded for DeepSeek; the schema-hash bump covers it.

Changes made to the document during review:

1. Prompt deferral now cites the decisive facts: `PROMPT` already says "Return
   only a JSON object", bodies already set `response_format=json_object`, and
   `PROMPT_HASH` is pinned to an exact digest in an existing test and feeds the
   dedup identity — so prompt strengthening is not trivially safe and stays
   deferred.
2. Test plan now names both existing sites asserting the legacy five-key `[]`
   summary (`shape_cases()` and
   `test_extra_wrapper_and_non_dict_shapes_fail_closed`), states that only the
   list branch of `_shape_summary()` changes, and makes the hash regression
   concrete (pin the actual pre-change hash with `shape_version="missing-null-1"`).
3. The open network-constraint question is resolved with verified facts:
   `tests/test_egress.py` is loopback-only (127.0.0.1 servers, public-looking
   destination remapped to loopback, non-loopback proxies policy-rejected, one
   test asserts all connection attempts are loopback), so the full suite
   satisfies the no-external-network constraint and must be run in full.
4. Risks section now enumerates the three verified `SCHEMA_HASH` consumers
   (runner dedup key, report `schema_prompt_mismatch` exclusion, planner
   done-matching) and their re-charge/re-plan consequences after the bump, and
   records that no production code reads individual `schema_shape` keys, so the
   two-key list summary breaks no code path.

No design change was needed to the acceptance rule, metadata contract, or
validation boundaries; all corrections were precision and provenance fixes.
Implementation may proceed per this document.

## Build verification

2026-11-18: implemented one-level singleton-dictionary unwrapping in `response()`,
the exact two-key rejected-list summary, and the `missing-null-listwrap-2` hash
version. `validate()`, prompt text/hash, and request bodies remain unchanged.
Regression coverage includes object/wrapped-object parity, non-verbatim value
rejection, missing/extra keys, rejected list forms, metadata clearing, response
guards, primary/fallback persistence, credential exclusion, and call/cost accounting.
New runner shape cases block socket connections and use injected transports and
temporary state.

The initial bare pytest invocation could not import the uninstalled `src` package.
Using the repository README's `PYTHONPATH=src:tests` setting, the full root command
`python3 -m pytest -q` passed: **329 passed, 391 subtests passed**.
No external network calls, live database/crawler operations, runtime configuration
changes, or site-file changes were made.
