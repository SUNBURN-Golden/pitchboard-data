# BUILD REVIEW: optional DeepSeek Flash shadow extraction

Reviewed 2026-11-05, `feat/deepseek-extract-shadow` vs `main`: single commit
`8bd02b6` ("Add disabled DeepSeek extraction shadow experiment and guard
tests"), 14 files, +2,539 lines — all new except an 8-line README section. No
existing collector, profile, schema, cron, workflow, or ops file is modified;
`src/pbdata/collect/shadow/` is imported by nothing outside itself.

Method: full read of the diff against the guard list, cross-checks against the
existing `net`/`parse`/`model`/`profiles` code it reuses and against the real
`/workspace/pb-eval` tree (read-only), re-running the tests and the offline
scorer, and re-verifying the provider contract against the live DeepSeek docs.
Nothing outside this worktree, `/tmp`, and read-only handles was touched;
PID 18143 (`bash /workspace/pitchboard/crawler/daemon.sh`) was only observed
(`ps`), still running, and its files, cron, and data roots are unmodified.

## Guard verification (all required guards present and effective)

| Guard | Implementation | Evidence |
| --- | --- | --- |
| Off by default behind config flag | `config/llm_extract.json` committed with `enabled:false` and null window timestamps; `common.load_config` fails closed (missing/false/string/unknown-key/zero-cap/inactive/expired → fixed `Stop` reason) before credentials, state, fetch, or network. Literal `true` plus explicit ≤7-day UTC window required; pricing expiry (`valid_until_utc: 2026-11-12`) makes the shipped config unenableable after that date even if flipped. | `test_checked_in_flag_is_false_and_no_side_effects`, `test_missing_invalid_false_config...`, `test_invalid_duplicate_config_and_stale_rates`; committed value re-checked via `git show HEAD:config/llm_extract.json` → `false`. |
| Daily call cap + daily cost cap | `state.reserve` inserts the attempt plus a permanent ceiling charge (USD 0.3024576 = 1M input tokens @ $0.30/M + 2,048 output @ $1.20/M) in a committed SQLite transaction under `flock` **before** dispatch; either cap binding stops the day. Crashed/failed/timed-out attempts keep their charge; midnight crossing, backwards clock, config change between reservation and dispatch all stop. | `BudgetTests` (exact boundary, restart, UTC rollover, 8-thread and 10-process contention, `test_independent_processes_share_budget`); effective cap 16 calls/day, 112/week reproduced by arithmetic and `test_default_five_dollars_allows_sixteen_calls`. |
| Key from env or `~/.config/pitchboard/env`, never printed/logged | `common.load_key`: env first (explicit empty rejected, no fallback), else a single safely parsed `DEEPSEEK_API_KEY`/`export` line from a 0600, non-symlink, nlink-1, current-user file; shell metacharacters rejected, file never sourced. Key goes only into the fixed-endpoint `Authorization: Bearer` header; no proxies (`ProxyHandler({})`), no redirects (`NoRedirect`), no retries. `key in dumps(body/raw/record)` tripwires; all provider/transport exceptions collapse to fixed reason codes. | `SecretAndIsolationTests`: sentinel key asserted absent from stdout/stderr, artifacts, request body; env-file parsing/permission/symlink matrix; `test_key_echo_in_response_is_rejected`; `test_no_secret_in_outputs_errors_logs_or_request_prompt`. |
| robots.txt respected; blocked sites skipped; no bypass | `policy.PolicyFetcher.allowed`: explicit status table (200 text/plain parsed with strict UTF-8 and sanity checks; 404/410 allow; everything else — 3xx/401/403/429/5xx/empty/non-text/oversize — deny), per-origin 1h cache, `can_fetch` for the collector UA plus robots crawl-delay and a ≥2s per-origin throttle. Blocked hosts from `config/blocked_hosts.txt` plus public-IP rules applied before DNS; DNS answers all checked public and the connection pins the vetted numeric address (no rebind, no `PITCHBOARD_EGRESS_GATEWAY_CIDRS` exception); every redirect hop re-canonicalized and re-checked. Page-level denials (401/402/403/429/451/5xx), challenge/login/password walls (`access_check`), non-HTML, compressed or oversized transports are terminal — no rendering, mirrors, or fallback. | `PolicyTests`: robots status table, caching, redirect→Instagram and redirect→new-origin robots checks, DNS pinning/gateway test, page denial and wall matrix, transport cap/compression tests. |
| Never touch Instagram | `canonical_url` hard-denies `instagram.com` and every subdomain (case, trailing dot, ports, userinfo, percent/backslash/control encodings rejected first) independently of the editable blocklist, before any DNS/robots/page request, on requested URL, every hop, and on cached pages via `Page.doc()` re-validation; eval-corpus Instagram URLs become `blocked_host` exclusions. `notinstagram.com` correctly not blocked. | `test_instagram_and_blocked_domains_never_dns_fetch_or_llm`, `test_redirect_blocks_instagram_before_request...`, `test_cached_policy_envelope_rechecked`, `test_cached_denials_and_instagram_are_exclusions_never_refetched`. |
| Never write to the live DB; shadow output only | The package imports no `db`/`store`/`promote`/`scheduler` module (grep-verified); the only SQLite handle is the private `<shadow-root>/budget.sqlite3`. `shadow_root` rejects the live root, both worktrees, the eval root, ancestors/descendants, symlinks; `secure_open` rejects symlinks (O_NOFOLLOW), non-regular/multi-link/wrong-owner/group-world-permissive files; dirs 0700, files 0600; `PB_DATA_ROOT`/`PB_REPO`/cwd ignored. Output is exactly `<shadow-root>/data/shadow/llm_extract_YYYYMMDD.jsonl`; partial tail lines stop the run rather than being repaired. | `test_no_live_db_store_promotion_scheduler_or_runtime_environment`, `test_protected_roots_symlink_hardlink_and_unrecognized_root`, `test_missing_corrupt_ledger_and_partial_output_fail_closed`; real run wrote only `/tmp/.../{experiment.json,budget.sqlite3,comparison.json,data/}` with 0700/0600. |
| Comparison script, eval read-only | `scripts/compare_llm_extract.py` defaults to offline `score` (no key lookup, no sockets, no eval-script execution); corpus readers enforce frozen checksums (`split_frozen.sha256`, `manifest_ho.sha256`), containment, symlink and mtime/size TOCTOU checks, `sha`-is-URL-hash contract with locally computed body hashes; missing bytes are exclusions, never refetches; tune URL/body overlap excluded; cross-cohort overlap reported and counted once. | `test_frozen_checksums_path_traversal_symlinks_and_input_changes_fail_closed`, `test_score_entrypoint_offline_no_secrets_live_db_or_eval_writes`, `test_cached_denials_...`, `test_generate_disabled_exits_before_eval_or_key_reads`; independent before/after tree-hash snapshot of `/workspace/pb-eval` in this review (below). |
| Crawler PID 18143 / cron untouched | Diff adds only standalone files; nothing schedules, signals, imports, or configures the running collector; the design notes the eventual integration is deliberately deferred. | Diff inspection; `ps -p 18143` observed only. |

## Independent verification runs (this review)

* `PYTHONPATH=src python3 -B -m pytest -q tests/test_llm_extract.py tests/test_llm_compare.py` → **46 passed, 89 subtests** (matches the BUILD doc). `PYTHONPATH=src:tests python -m unittest tests.test_llm_extract tests.test_llm_compare` → OK (README invocation also works).
* Full suite `PYTHONPATH=src python3 -B -m pytest -q` → **148 passed, 89 subtests, 3 failed** (`test_collector.py` image-OCR, scanned-PDF-OCR, E2E) — exactly the BUILD doc's claim; `tesseract` is confirmed absent locally, and none of the three touch the shadow package.
* Offline scorer against the real eval tree: `python3 -B scripts/compare_llm_extract.py --eval-root /workspace/pb-eval --shadow-root /tmp/<fresh>` → `eligible_pages: 188`, `scored_predictions: 0`, deduplicated 186; exclusions 183 `access_wall`, 47 `cached_fetch_denied`, 38 `tune_overlap`, 27 `page_too_large`, 13 `unsupported_content`; 2 cross-cohort overlaps (both d1102 URLs, primary correctly d1030). All numbers reproduce the BUILD smoke run exactly. A full `find /workspace/pb-eval -type f -printf '%p %s %T@' | sort | sha256sum` snapshot before and after the run was **identical** — the eval tree is read-only in practice, not just by intention.
* B3 gold contract: the hardcoded heading sets in `compare.py` match `d1030/b3_gold.json` verbatim, and the mapping reproduces exactly **22 labeled URLs / 7 writing** against the real `manifest_b3.json`. `split_frozen.sha256` and `d1102/manifest_ho.sha256` cover exactly the file sets the checksum verifier requires.
* Baseline projection: `runner.baseline` was diffed line-by-line against `/workspace/pb-eval/d1102/run.py::promote_calls` and `collect/promote.py::promote_raw` call creation — the gate logic is equivalent in all four recruitment/rules cases (the eval replay's `call_gate` hook does not exist in this repo, so its absence from the baseline is faithful).
* Provider contract re-verified against the live DeepSeek docs on 2026-11-05: `thinking: {"type": "disabled"}` is a valid parameter; `deepseek-flash` is a valid model id; `response_format: {"type": "json_object"}` is valid and its required JSON-in-prompt instruction is satisfied by `PROMPT`; `max_tokens` bounds are respected. Pricing page confirms **$0.30/M peak cache-miss input and $1.20/M peak output** — exactly the config ceilings, with cache-hit/off-peak discounts deliberately not credited (conservative).
* `git diff --check main...HEAD` clean.

## Findings

None of these are guard violations; all are optional hardening or disclosure
items. No correctness bug was found in the enforced paths.

1. **Score-only runs are not footprint-free on the shadow root** (`scripts/compare_llm_extract.py:28`). Even with no `--predictions` and a disabled config, `State(...)` initializes `experiment.json`, `experiment.lock`, and `budget.sqlite3` in `--shadow-root`. No API calls or eval writes result, but a typo'd root gains an experiment identity. Related: `State()` uses `mkdir(parents=False)`, so a root whose *parent* is missing raises a non-`Stop` exception that the script's `except Stop` handler does not catch — a raw traceback instead of the fixed-reason JSON protocol. Robustness nit only.
2. **Malformed prediction rows crash with bare `KeyError` instead of `Stop`** (`compare.py:212`, `compare.py:283`). `predictions()`/`score()` index `row["requested_url"]`/`proposals[f]["value"]` without shape checks. Rows written by this tool always have the right shape, so this only affects hand-edited or foreign JSONL — it fails loudly, never silently, and leaks nothing; but it escapes the documented fail-closed reason-code protocol.
3. **`generate` loop breaks only on `budget_exhausted`** (`scripts/compare_llm_extract.py:64-65`). Terminal states like `experiment_halted`, `disabled`, `expired_pricing`, or `unrecognized_shadow_root` iterate the remaining sample making zero provider calls — wasted time, no spend.
4. **`boolean_metrics["accuracy"]` mixes denominators** (`compare.py:231`): numerator counts matches among covered pairs, denominator is all labeled pairs (abstentions count as wrong), while `precision`/`recall` treat abstentions as misses only for positives. The end-to-end reading is the strict one and the report notes cover it, but the field name invites a covered-subset reading. Consider renaming (e.g. `accuracy_end_to_end`) in a later revision.
5. **`max_page_utf8_bytes` duplicated as the literal `24000` in cohort eligibility** (`compare.py:174`). If the config value were ever lowered, some cohort-eligible pages would be skipped pre-reservation by the runner (no budget impact — the runner checks size before `reserve`), and the two thresholds could silently diverge.
6. **Labeled-sample power is small after access screening** (measurement, disclosed): applying the wall policy to cached pages excludes 183 candidates, including **all four ltikorea B3 writing positives**, leaving 18 labeled B3 pages = 3 positives / 15 negatives. The real-call gold comparison is therefore backed by only 3 positive labels. The design discloses this; any read of the week-1 report should repeat it, and a human decision on whether those four public board pages constitute real walls (they may only carry a login link in boilerplate) belongs in the run stage, not in code.
7. **First enabled run should be a one-URL smoke test.** The returned-model allowlist is exactly `{"deepseek-flash", "DeepSeek-V4.1-Flash-1017"}` (`schema.py:146`); a changed provider alias durably halts the experiment (conservative, bounded). Conversely a 4xx/5xx provider failure is charged but does *not* halt — a bad key would burn the day's ceiling ($5 max) in failed attempts. Both are per design; a single-URL first run cheaply validates identity/params before the sample loop. (This review's live docs check already de-risked the request parameters.)
8. **Minor doc nuance**: `timeout_seconds` governs only the provider call; page/robots fetches hard-code a 15s timeout (`policy.py:148`). Worth a line in the design doc's config table if anyone tunes timeouts.

Positive notes worth keeping: the credential tripwires (`key in dumps(body|raw|record)`), the offline-cached/live provenance split that makes it structurally impossible for eval generation to claim robots-verified fetches, the permanent-reservation cost model (no refund paths to get wrong), and the reconstruction-and-revalidation of predictions at score time (a JSONL row is re-proven against the cached page before it can move a metric) are all stronger than the brief required.

## Verdict

The branch satisfies every stated guard with tests, the disabled flag is the
committed state, and the isolation boundary held under independent verification
(read-only eval hash, real-run numbers reproduced, live crawler untouched).
**Approve for the shadow-experiment stage as-is.** Findings 1–5 are optional
hardening; 6–8 are operational guidance for the first enabled run. Any live
generation remains gated on explicitly enabling the config with a fresh pricing
verification, and must never be merged or pushed toward `main`.

## REVIEW FIX pass (2026-11-05)

All findings were addressed in the follow-up commit. No guard or enabled path
was relaxed.

1. **Score-only footprint / missing parent.** Score mode no longer constructs
   `State`: it validates the root with `state.comparison_output` and writes only
   `comparison.json`, creating no `experiment.json`, lock, or budget ledger.
   `common.shadow_root` now converts a missing/unwritable parent into
   `Stop("unsafe_root")`, so the script's fixed-reason JSON handler covers it
   instead of leaking a traceback. Tests:
   `test_score_only_has_no_experiment_identity_and_missing_parent_is_stop`.
2. **Malformed prediction rows.** `compare.predictions` and `compare.score`
   now shape-check rows and proposals and raise `Stop("invalid_prediction")` /
   `Stop("invalid_prediction_fields")` rather than bare `KeyError`/`TypeError`.
   Test: `test_malformed_prediction_rows_raise_fixed_stop_not_keyerror`.
3. **Terminal generation states.** The loop breaks on any terminal result
   (`experiment_halted`, `disabled`, `expired_pricing`, budget/config/state
   failures, model/usage halt) and continues only for known page/host-local
   skips. Test: `test_generate_loop_treats_experiment_states_as_terminal`.
4. **Accuracy denominator.** `boolean_metrics["accuracy"]` was replaced by
   explicit `accuracy_end_to_end` (abstentions wrong) and `accuracy_covered`
   (covered subset only). Test: `test_hand_computed_scores_and_abstentions`.
5. **Duplicated page threshold.** `common.page_limit` is the single reader of
   `max_page_utf8_bytes`; `compare.load_cohorts` requires the limit and the
   scorer passes the configured value. Test:
   `test_cohort_threshold_shares_config_and_rejects_invalid`.
6. **Low labeled power.** The scorer emits a permanent report note and the
   design doc records the observed 3-positive/15-negative B3 state and the
   human wall-adjudication decision. No code attempts to reclassify walls.
7. **First enabled run.** The design doc now requires a fresh-root,
   `--sample-size 1` smoke test before the sample loop, explaining the immutable
   frozen sample, the model allowlist halt, and charged non-halting 4xx/5xx
   failures.
8. **Timeout nuance.** The design doc now states that `timeout_seconds` applies
   only to the provider call; page/robots fetches use the fixed 15-second
   transport timeout.

Verification after the fix: focused suite
`PYTHONPATH=src python3 -B -m pytest -q tests/test_llm_extract.py tests/test_llm_compare.py`
passes, and the committed config remains `"enabled": false`.
