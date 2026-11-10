# DeepSeek shadow improvement design

Stage: **DESIGN only**, 2026-11-10. This change documents proposed work; none of
the interfaces below is implemented by this stage. Implement the five stages in
the order listed, with affected pytest checks and a commit for each stage on
`feat/deepseek-shadow-target-gaps`. Never push to `main` or force-push.

## Boundaries and starting evidence

- Keep checked-in `config/llm_extract.json` at `enabled=false`. Runtime activation
  belongs exclusively to the existing isolated runtime and private configuration.
- Do not modify or run the live crawler, collector/daemon cron, HANSET cron,
  Instagram collection, site, newsletter, or production content paths. SOULBOUND
  is excluded from targets and work. Do not edit `~/.config` from this repo.
- `/workspace/pitchboard/data-root/service_db` is protected. Future planning may
  read an explicitly selected SQLite file read-only; all outputs remain under the
  validated shadow root. No service migrations, gap updates, promotion, fact or
  evidence writes. Preserve the 2026-10-27 retention decision: `pb_raw_ref` keeps
  hashes/metadata, and `pb_evidence` keeps sanitized excerpts of at most 280 chars.
- Preserve robots, access-block, redirect, network, credential, permissions,
  locking, permanent reservation, halt, and pricing/window checks. No retry or
  alternate access path for a denied page. Never print keys, headers, provider
  error bodies, page bodies, or arbitrary exception messages.
- The user reports approximately 120 open `not_extracted` gaps, 30 fee gaps,
  fee coverage near 5/35, and an isolated daily job with five hardcoded URLs.
  These are planning estimates, not measurements made in this worktree.

Inspected implementation: `runner.py` already sends parsed page text (up to
24,000 UTF-8 bytes), computes a pure rules baseline, and records accepted quotes.
It does **not** persist reusable page text. Service excerpts alone cannot recover
that text. `schema.py` currently supports `open_call`, `deadline`, `pay`, and
`eligibility`; submission fee is missing and must be added explicitly. Existing
quote checks enforce segment-local verbatim evidence but do not prove all value
tokens. `State` provides one shared ledger and recoverable extraction JSONL.
`compare_llm_extract.py` defaults to offline held-out scoring.

The present reservation is USD 0.3024576 per attempted call using the checked-in
ceilings, including failed calls. Thus the default USD 5 cap allows 16 calls/day,
although `daily_call_cap` is 50. Forty URLs is a selection limit and operational
target, never a promise to bypass the dollar cap. No current provider pricing
verification is claimed here; all dollar examples use the repository snapshot.

## 1. Gap-targeted planner and batch runner

Proposed files: `collect/shadow/planner.py`,
`scripts/llm_extract_shadow_batch.py`, and focused planner/batch tests. Keep the
single-URL CLI and `ShadowRunner.run` compatible. Use one runner, shared
`PolicyFetcher`, shadow root, and ledger for batch, single-URL, and explicit eval
generation. Sharing the fetcher preserves host throttling and host halts.

### Inputs and relationship resolution

Require exactly one of `--gaps-json PATH` or `--service-db SQLITE_FILE`. Do not
discover live paths through `PB_DATA_ROOT`, instantiate a collector, or import
the write-capable service connection/migration helpers. An explicit `--plan-only`
mode reads inputs and produces a plan without credentials, sockets, or a budget
reservation. Execute mode first validates enabled config/window before reading
the input database, fetching pages, or creating state.

The DB adapter uses a correctly escaped SQLite URI with `mode=ro`,
`PRAGMA query_only=ON`, a short consistent read transaction, and fixed SELECTs.
Reject unexpected schema, symlink input paths, and unknown subject types with
fixed reasons. Do not use `immutable=1` on a changing live database, change journal
mode, checkpoint, migrate, or create WAL/SHM files. A filesystem read-only guard
must prevent sidecar creation/writes too; if the active journal layout cannot be
read without writes, stop and use an operator-supplied consistent export. Close
the reader before any page/provider work. DESIGN does not open the live DB.

There is no `pb_gap.raw_id`. Resolve relationships as follows:

1. Select `pb_gap.state='open' AND gap_class='not_extracted'`.
2. For an outlet subject, use `subject_id` as outlet ID; for a call subject, join
   `pb_call_edition.call_id` to obtain `outlet_id`.
3. Join `pb_source.owner_outlet_id` to related `pb_raw_ref.source_id`. Use the
   latest raw observation per URL, not the latest successful observation if a
   newer observation is denied. Initially accept HTML with `outcome='ok'` and an
   allowed source permission. PDF/image support is outside this change.
4. Prefer a page with `pb_extraction.field='signal.cue'` whose `value_json.for`
   equals the predicate, then a relevant recruitment/rules page, then another
   eligible page belonging to the outlet. Do not join unrelated pages by hostname
   alone. Outlet association is a discovery relation, not proof of call scope.
5. Carry raw/source/gap/subject IDs, scope, predicate, importance, observation
   time, permission, URL, final URL, and available content/text hashes into the
   plan. Record the relationship basis. Start from `pb_raw_ref.url`; recheck every
   actual redirect through the existing policy. Never follow canonical hints.

The JSON export has a versioned envelope (`schema`, `exported_at_utc`, `rows`)
with one row per related gap/raw candidate containing the same metadata and
relationship basis. It contains no bodies, credentials, or arbitrary SQL. Strict
types, bounded size, hash/date validation, and the same eligibility checks apply
to both inputs. Fresh exports should be supplied for each daily plan; record the
snapshot age and reject exports older than 24 hours in execute mode. Plan-only
may inspect stale exports with an explicit stale flag. No DB read is needed when
JSON is supplied. Missing eligible URLs produce counts/reasons, not random fills.

### Ordering, deduplication, and dispatch

The native fee predicate is `fee.submission`; normalize only documented export
aliases such as `submission_fee` to it. Order fee gaps first, followed by supported
weak fields (`deadline.closes_on`, `compensation.rate`, and eligibility predicates).
Report unsupported predicates rather than spend calls on fields the schema cannot
extract. Within each tier, prefer unattempted/changed source hashes, higher gap
importance, older shadow attempt time, then stable gap ID and URL. Round-robin
outlets within equal priority to avoid one site occupying the batch. Carry all
gap targets when deduplicating canonical URLs; do not deduplicate distinct URLs
solely because their text hashes match. Do not mutate live gap attempt counters.

`--limit N` defaults to 40 and accepts 1–50. Select at most N distinct URLs;
page-local failures do not cause unlimited backfill. Honor the remaining shared
UTC call and dollar budgets; a 40-URL batch after 20 calls can dispatch at most
30 calls. Existing same-page/day/prompt/config deduplication remains authoritative.
Prefer previously unprocessed hashes on subsequent days so unchanged fee pages
do not permanently starve other gaps. A shadow plan/history records selection,
skips and attempts; it does not assert that a live gap was filled.

Move the existing explicit page-local continue/terminal classification into a
shared helper used by both batch and eval generation. Continue only known
page-local denials/skips and documented non-halting provider outcomes; stop on
budget exhaustion, disabled/changed config, expired window/pricing, clock
changes, model/usage halts, unsafe/corrupt state, or unknown outcomes. Apply this
to both `outcome` and `reason`, including failures after a reservation. Preflight
caps/halts before the next fetch, then recheck and reserve atomically immediately
before dispatch. Do not refund failed/crashed reservations or retry provider calls.

Initialize experiment identity under the existing state lock before writing new
plan/cache files to a fresh root; otherwise `State` rejects the populated root.
Plan-only output must not accidentally populate an uninitialized execution root.
Stdout contains counts, fixed reason codes and opaque IDs only. Detailed plans
live in private shadow files. A new day's batch requires a new invocation; a
midnight boundary stops the current batch rather than extending its allowance.

## 2. Bounded reusable source text

Proposed `collect/shadow/source_cache.py` owns
`<shadow-root>/data/shadow/source_text/<cache-key>.json`. The key combines canonical
requested/final URL, source body hash, extracted-text hash, parser version, and
cache-format version. Preserve distinct names for raw body hash and parsed text
hash: service `text_hash` may use a different normalization and cannot be assumed
identical to `digest(doc.text())` without a versioned conversion contract.

Persist extracted text and its segment boundaries, indices, kinds, headings,
minimal title/language/metadata needed by the pure baseline, and policy provenance
(requested/final URLs, redirect chain, fetch time and policy version). Include
hashes for the payload and source, observed raw ID/hash, byte length and
`truncated=false`. Store no HTML, links to traverse, or body in the service DB.
Both the baseline and LLM must use this same reconstructed Doc and `as_of` date.

Use the existing `max_page_utf8_bytes`, with an absolute 24,000-byte text ceiling.
Keep the current reject-on-oversize behavior for this increment: do not silently
truncate a page and infer absence or closed status from an incomplete document.
Separately bound metadata, segment count, and the serialized cache envelope
(proposed 128 KiB maximum), including title/headings. Unicode round trips must
preserve exact text, offsets and hash. Oversize input remains an explicit exclusion.

A cache hit requires matching URL and the requested known source hash, intact
payload hashes, compatible parser/policy versions and size limits. Recheck current
host exclusions and known source denials on every reuse. Record provenance as
`shadow_cached` with the original fetch timestamp; never claim a new robots check
or a fresh observation. A valid matching copy requires no page fetch, DNS, or
robots request. Historical replay cannot certify current source status. If the
latest exported observation reports a denial, do not reuse an older success.
Without a known matching hash, perform the normal policy-checked fetch; on a
changed body, create a new cache entry and flag `source_changed` against the plan.
Never pretend that changed text fills a gap for the older revision.

Use a dedicated validated cache-to-Doc path; do not fabricate HTML or accept a
caller-forged `Page` as `live_verified`. Missing/stale copies can trigger a normal
fetch; hash corruption, unsafe paths or malformed existing cache entries stop
with a fixed reason and no fallback that conceals the corruption. Reuse cached
text for extraction, offline revalidation and conflict review. Bump prompt/schema
hashes for fee/token changes so old successful rows cannot masquerade as new output.

Write atomically with fsync under the existing lock; directories 0700, files 0600,
same ownership/symlink/hardlink protections as existing outputs. Check for the
loaded credential before persisting text or any new record, just as for prompts
and responses. Redact contact details from evidence/review output; the private
source copy preserves exact source bytes and is never echoed to logs or committed.
Declare cache retention explicitly: proposed expiry 30 days after observation,
with any cleanup confined to these cache files. Missing expired text is an
unverifiable result during scoring, not permission to refetch. Ledger, extraction
and review history are never deleted to reset limits.

## 3. Acceptance, conflicts, and second opinions

Proposed changes: versioned additions in `schema.py`, a pure comparison helper,
and `collect/shadow/review.py`. Add a separate `submission_fee` proposal mapped to
`fee.submission`, with value keys `text`, `state`, `amount_min`, `amount_max`,
`currency`, `unit`. State is `stated|explicitly_none|unknown`; unknown amounts stay
null, and explicitly-none requires an explicit fee waiver/no-fee statement.
Payment instructions can establish a fee without inventing an amount. Keep fee,
author compensation and prizes separate. Version-1 predictions remain readable
under their original validator with fee marked unsupported; do not rewrite them.

Acceptance remains field-local: reject only the unsupported field, save fixed
validation reasons, and keep other validated fields. Every retained field needs
a 1–280-char exact quote in one stored source segment. In addition:

- Free-text value and constraint strings must be exact source spans, supported by
  the quoted segment. Evidence from unrelated sentences/calls is insufficient.
- Critical amounts, range endpoints, currency/unit cues, complete date parts
  (including year), timezone, and eligibility qualifiers must occur verbatim in
  the quote. Require token boundaries: `20` is not supported by `2026`.
- Typed values such as ISO dates and enums are derived only through explicit,
  tested deterministic mappings from those verbatim lexical tokens. For example,
  `20 November 2026` can support `2026-11-20`; `November 20` cannot supply a year.
  Currency symbols or Korean amount units need an unambiguous supported mapping;
  otherwise normalized components stay null. No inferred currency or timezone.
- Open/closed/not-call enums require a supporting assertion and temporal/context
  consistency, not just words occurring somewhere in the page. Null on ambiguous
  scope, multiple calls, contradictory periods, or unsupported negation. Do not
  treat missing information as false, free, unpaid, closed or not-call.

The validator is evidence support, not a proof that semantic classification is
correct. Keep human adjudication in the accuracy workflow.

Compare accepted proposals with the recorded pure baseline on the **same page
hash, scope and as_of**, never with a different revision's service facts. Compare
all baseline candidates as sets, preserving ambiguity. Match decimal values and
dates with deterministic normalization while retaining original evidence. A null
baseline plus a supported value is a candidate fill; two supported incompatible
values are a conflict. Partial/unknown values or incomparable scopes are review
reasons, not automatic equality or inequality. Include fee, compensation,
deadline, eligibility and call status in the comparison.

Append derived records only to
`<shadow-root>/data/shadow/review_conflicts_YYYYMMDD.jsonl`, containing a stable
review ID, attempt/gap IDs, URLs and hashes, as_of, predicate, rules candidate
values/quotes, accepted LLM value/quote/offsets, reason codes, and
`second_opinion_required`. A conflict stays in shadow; nothing auto-promotes or
closes a live gap. A second opinion means a review flag, not another provider call.

Flag both directions: rules current-open versus LLM closed/cancelled/not-call;
rules real-call versus LLM not-call; rules closed/not-real versus LLM open/real.
Use explicit labels/deadline evidence to distinguish an actual negative baseline
from missing rules coverage. Unknown/null classifications are abstentions and
should have a separate uncertainty reason. Preserve both opinions; do not elect a
winner by confidence. Conflicts involving a rejected LLM value remain validation
failures, not accepted disagreements.

Make review publication recoverable from the durable completed attempt, with
stable IDs based on attempt/predicate/reason/version. A crash after extraction but
before review publication must replay the review without another API call or
duplicate row. Fail closed on partial JSONL tails, as existing extraction output
does. Cache/review failure must never cause a second provider reservation for the
same completed attempt.

## 4. Optional digest drafts

Proposed `--digest-languages ko,en` enables one short line per requested language
for a successful extraction with usable accepted fields. Default is off. Use
deterministic Korean/English templates over accepted structured fields initially,
so drafts add **zero provider calls** and cannot invent free-text translations.
Keep source proper names as supplied; translate only fixed template vocabulary.
Skip unresolved conflicts, second-opinion cases and unsupported content rather
than publish a confident summary. No accepted fields means no draft.

Write only to
`<shadow-root>/data/shadow/digest_drafts_YYYYMMDD.jsonl`, with stable draft ID,
attempt/hash references, language, one line (at most 280 chars), supporting field
IDs and `draft=true`. No contact details or unverified facts. Use the same
recoverable/idempotent derived-output mechanism as reviews. No site, newsletter,
production path or publishing integration. A future model-generated paraphrase
would require separately budgeted dispatch and evidence checks; it is not part
of this implementation.

## 5. Offline accuracy and cost report around 2026-11-14

Extend `compare_llm_extract.py` with an explicit shadow-run input mode, retaining
the existing held-out path and default `score` operation. Proposed options:
`--input-mode shadow --predictions ... --gold-json ... --ledger ... --as-of ...`.
Read extraction JSONL, stored source snapshots, recorded baselines, and an optional
human-labeled manifest. No provider import/dispatch, credential lookup, page fetch,
live DB access, or budget state initialization in score mode. Outputs go only to
the validated shadow root; ledger reads use the same strict read-only safeguards.
Write `comparison_YYYYMMDD.json` and a concise Markdown report.

Match rows on canonical URL, body/text hashes, as_of, and schema/prompt versions;
revalidate evidence against the matching cache text. Validate row schemas and
reject conflicting duplicates. Report legacy, missing-text and invalid-evidence
rows separately, never silently count them as correct. Freeze cohort membership,
input checksums, baseline/extractor versions, runtime commit, and scoring commit
for reproducibility. Count a page once per evaluation cohort; show repeat attempts
separately for cost and longitudinal coverage.

Report the following with explicit denominators:

- Selected/eligible/attempted/successful pages, exclusions, missing predictions,
  supported gap targets, accepted candidate fills and field coverage by predicate,
  language and sampling tier. Fee coverage must have its own denominator.
- Rules-versus-LLM agreement, conflicts, and second-opinion counts. Agreement is
  not accuracy. A baseline is not gold; if no independent labels exist, emit
  `accuracy=null` with `missing_gold` and still report coverage/agreement/cost.
- With frozen human labels, compare both methods to identical gold: real-call
  and current-open confusion counts, precision/recall/F1, covered accuracy,
  end-to-end accuracy (abstentions incorrect), and field-level precision/recall
  for fee/deadline/pay/eligibility. Gold records include URL/hash/as_of, scope,
  expected values and evidence, reviewer and review time. Include rule negatives
  and agreements as well as conflicts; never label only the review queue.
- Keep gap-targeted and held-out cohorts separate. Targeted coverage is biased
  toward weak fields and cannot establish population accuracy. Report small
  labeled samples and missing labels explicitly; do not extrapolate the reported
  5/35 coverage into an accuracy claim or assume all 40 daily selections succeed.
- Reconcile every ledger attempt, including failures and unfinished reservations,
  against output IDs. Sum Decimal permanent charges by UTC day, config/pricing
  snapshot and experiment; show configured caps, remaining calls/dollars,
  unknown usage, halt reasons and discrepancies. Successful JSONL alone understates
  cost. Compute optional token-based usage estimates separately from the permanent
  ceiling and label them as estimates, not invoices or refunds. Missing ledger
  means cost completeness is unknown; multiple roots do not provide an account cap.

Prepare a frozen snapshot around 2026-11-14 and rerun the same offline report at
the end of any renewed private window. Human decisions remain shadow annotations;
this report does not trigger activation or promotion.

## Operator handoff after implementation

These are future operator steps, not commands executed by DESIGN. Existing
activation/background details remain in [LLM_EXTRACT_ACTIVATE.md](LLM_EXTRACT_ACTIVATE.md).

1. Verify the implemented branch and offline tests. Keep the existing isolated
   runtime/root/ledger; do not use a new root to reset daily usage. Produce a fresh
   metadata-only gap export through an authorized read-only workflow.
2. Run plan-only against the export, inspect counts/fee priority/exclusions, then
   a one-URL batch smoke test with the existing private config. Confirm replay,
   fees, review output, caps and fixed-reason logging before raising the limit.
3. Replace only the isolated shadow job's five-URL command with the proposed
   invocation below, keeping its schedule, non-overlap lock and private log
   permissions. This document neither edits cron nor supplies a cron installer.
   Leave collector/daemon, HANSET, Instagram and all other jobs untouched.

   ```sh
   python3 -B scripts/llm_extract_shadow_batch.py \
     --config /absolute/private/llm_extract.json \
     --shadow-root /absolute/existing/shadow-root \
     --gaps-json /absolute/private/gaps.json --limit 40
   ```

   Paths are placeholders; the command is available only after implementation.
   Export refresh failure must stop the job rather than reuse stale input or fall
   back to the old hardcoded URLs. All invocations share the same ledger.
4. Keep `daily_call_cap=50`. At repository-snapshot prices, 40 reservations cost
   USD 12.098304 and 50 cost USD 15.122880; other attempts consume that headroom.
   An operator may select a sufficient enforced private dollar cap (USD 16 covers
   50 at these ceilings), or retain an already authorized private
   `monetary_cap=none`/`daily_cost_cap_usd=null` setting. Do not silently disable the
   monetary cap or lower the conservative reservation. With USD 5, stop at 16.
5. Re-check official [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/)
   before renewing the private window. To run through the exclusive endpoint
   `2026-11-21T00:00:00Z`, renew on 2026-11-14 with start
   `2026-11-14T00:00:00Z`, end `2026-11-21T00:00:00Z`, verified date `2026-11-14`
   and pricing validity through that end. Update ceilings if required. Do not
   extend the original start into a greater-than-seven-day window or set a future
   verification date early. Continue using the same ledger across renewal; do not
   reset state. No automatic extension, repo config enablement, or secret edits.
6. Monitor counts/cost/halts and review files in the isolated runtime. To stop,
   disable only its private config/job through the operator workflow. Preserve
   ledger and outputs for offline comparison. No production rollback is involved.

## Verification and implementation acceptance

All future tests use synthetic SQLite fixtures, synthetic pages/keys, temporary
shadow roots, fake clocks and provider stubs. Network and secret-reader tripwires
must make accidental live activity fail. No tests query the live service DB.

| Stage | Required affected tests |
| --- | --- |
| 1 | Fee-first ordering; call/outlet joins and cue preference; newest denied raw excluded; JSON/DB plan parity; unsupported/stale inputs; URL dedup; SOULBOUND/Instagram exclusion before DNS; read-only SQL and unchanged DB/sidecar bytes/metadata; disabled no-side-effects; 40 default; shared 50-call and dollar caps under concurrent single/batch invocations; halt/expiry/config/UTC stops; no live gap updates. |
| 2 | Matching URL/hash reuse makes zero fetch calls; changed/missing/version-mismatched source; UTF-8 ceiling; no cross-segment offsets; baseline equivalence after Doc reconstruction; corrupt cache fails closed; denied-source cache rejection; secure paths and atomic recovery; no body in service DB/evidence/logs; credentials never persisted. |
| 3 | Verbatim quote plus token support; real quote/wrong amount/date/year/currency/negation rejected; fee versus compensation/prize; explicit-none versus absent fee; Korean/English amounts and qualifiers; two-way call-status flags; missing baseline versus conflict; ambiguous rules candidate sets; same-revision pairing; review recovery exactly once; legacy schema compatibility. |
| 4 | Off by default; Korean/English single-line length; only supported fields; no conflicting/empty drafts; zero additional provider calls; private-only destinations; duplicate/crash recovery; contact/credential filtering. |
| 5 | Hand-computed accuracy and abstention denominators; agreement never gold; field/fee metrics; cohort/hash/version mismatches; malformed and duplicate rows; missing cache/gold/ledger; failed and unfinished reservations; exact Decimal ceilings; offline subprocess forbids sockets/secrets/live DB; held-out regression. |

For each implementation stage, run the existing suites plus new affected tests:

```sh
PYTHONPATH=src python3 -B -m pytest -q -p no:cacheprovider \
  tests/test_llm_extract.py tests/test_llm_compare.py
```

Add planner/cache/review/digest/report test files to that invocation as introduced;
run `tests/test_fee_submission.py` when mapping fee semantics. Before every commit,
inspect the diff and `git diff --check`, verify branch and disabled checked-in
config, and ensure no runtime data or secrets are staged. Each implementation
commit must report its actual test results and remaining limitations.

DESIGN-stage validation is the existing extraction/comparison suite and a
documentation-only diff. It does not claim that the proposed behaviors have been
implemented or tested. No provider calls, live DB reads, cron/private-config edits,
or runtime activation are required to review or commit this design.

Validation on 2026-11-10: the command above passed **55 tests and 165 subtests**.
The checked-in config remains disabled. Only this design document is changed.
