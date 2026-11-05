# Optional DeepSeek Flash extraction: BUILD

Implemented on `feat/deepseek-extract-shadow`, 2026-11-05. The flag remains **false**.
No real provider calls, credential reads, live collector execution, deployment,
cron changes, or live database writes were performed during BUILD. Source changes
are confined to this linked worktree; PID 18143 and its runtime files are untouched.

## Implemented entry points

The initial integration is the standalone shadow lane specified in the design.
`ShadowRunner.run` reuses the web collector's HTML parser and pure Pitchboard
rules, then runs optional extraction alongside those rules. The active collector
imports none of this package. An LLM result cannot affect scheduling, promotion,
published facts, or the rules result. Installing a hook into the running collector
remains a separate, later change after the comparison.

* `scripts/llm_extract_shadow.py`: one policy-checked URL, one optional model call.
* `scripts/compare_llm_extract.py`: offline score by default; explicit `generate`
  uses cached held-out HTML and the same optional runner and budget.
* `src/pbdata/collect/shadow/`: config/credentials, fetch policy, evidence schema,
  pure baseline, permanent budget ledger, shadow output, and cohort comparison.
* `config/llm_extract.json`: checked-in `enabled=false`, 50 calls/day, USD 5/day,
  UTC accounting, 24,000 page-text UTF-8 bytes, 2,048 output tokens, 30-second
  provider timeout. There are no retries, environment enable switches, or tools.

Missing, disabled, malformed, unknown-key, inactive, or expired configuration
stops before credentials, feature fetches, state creation, and network access.
Activation requires literal JSON `true` and explicit UTC start/end timestamps
spanning at most seven days, covered by the pricing validity window. There is no
scheduled job or automatic activation. The shipped null timestamps cannot run.

## Provider contract and cost decision

Verified against official documentation on 2026-11-05:
[`deepseek-flash`, 1M context and peak prices](https://api-docs.deepseek.com/quick_start/pricing/),
[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/).
The fixed endpoint is `https://api.deepseek.com/chat/completions` with
`Authorization: Bearer`, JSON output, non-thinking mode, temperature zero,
non-streaming output, and no tools. TLS certificate verification stays enabled;
proxies and provider redirects are disabled. No provider error body is retained.
Accepted returned identities are `deepseek-flash` and `DeepSeek-V4.1-Flash-1017`.
A model mismatch or usage above the configured bounds durably halts the experiment.

BUILD chooses the full 1,000,000-input-token reservation and peak ceiling prices
USD 0.30/M input and USD 1.20/M output. Each attempt is permanently charged
**USD 0.3024576** in the local ledger. This ceiling charge is not an invoice cost.
Unlike the earlier settlement proposal below, successful calls do not release
unused reservations: timeouts, malformed responses, crashes and successful calls
all consume the same maximum. This deliberately simpler rule allows at most
**16 calls/day, 112/week**, despite the nominal 50-call cap. The generator freezes
a deterministic sample of at most 112 unique pages, round-robin by cohort/language,
including rule negatives. Unselected pages remain missing predictions.

One explicit private shadow root owns the experiment identity and shared ledger
for both live shadow and cached generation; use that same root for every run and
resume. Independent roots/machines do not provide account-wide coordination.
`flock` serializes processes, SQLite commits a call plus Decimal ceiling charge
before dispatch, and both daily limits must permit the reservation. Midnight
crossing or backwards time stops dispatch; uncertain charges are never reclaimed.
Deleting initialized state, corrupt state, unsafe outputs, or persistence errors
fails closed. Validated completed records recover from the ledger without another
API call. A partial JSONL tail stops the run rather than guessing how to repair it.

## Access, secrets, evidence, and output

The shadow fetcher checks canonical hostnames before DNS or requests and at each
redirect. Instagram and every subdomain are unconditionally denied, independently
of `blocked_hosts.txt`; the entire repository blocklist also applies. Canonical
hints and links are never followed. DNS answers must all be public; connections
pin a checked numeric address, retain TLS hostname validation, and have no egress
gateway exception. Each new origin/path passes robots for the collector UA.
Robots 404/410 allow; valid nonempty text/plain 200 is parsed; all other statuses,
redirects, errors, invalid bodies and disallow rules stop the attempt. Origin caches
last one hour. Requests honor a minimum two-second interval and robots crawl delay.
Page HTTP denials, challenge/login controls, non-HTML, encoded/oversized responses,
empty text, and excess redirects stop without any rendering or fallback.
Challenge detection is deliberately conservative and can exclude otherwise public
pages containing challenge controls; these exclusions remain visible in reports.

The key comes from `DEEPSEEK_API_KEY`, otherwise a safely parsed single assignment
in `~/.config/pitchboard/env`. The file is never sourced; it must be a regular,
non-symlink, current-user-owned private file. Shell expressions are rejected.
Empty explicit environment keys do not fall back. Keys never enter argv, prompts,
logs, error text or output; provider echoes of the key are rejected before storage.

All four fields (`open_call`, `deadline`, `pay`, `eligibility`) are always present
in a successful prediction. Each is null or an object containing `value` and a
1–280-character verbatim quote from exactly one parsed segment. Missing, fabricated,
normalized, cross-segment, contact-containing or invalid field evidence nulls the
**whole field**. Offsets, quote hashes and occurrence counts are computed locally.
Evidence uses `Doc.text()` exactly, including the title segment once. Dates require
an explicit year; amount strings use Decimal validation; classification/status
contradictions and a retained past deadline against an open classification are
rejected. Semantic support remains a model-quality question: exact evidence does
not prove a value is correct, and these predictions are never promoted.

Only a dedicated `0700` shadow root may hold state. The known live roots, both
repository worktrees, the eval root, their ancestors/descendants, symlink paths
and hard-linked output/state files are rejected. Files use `0600`. Runtime roots
are explicit and ignore `PB_DATA_ROOT`, `PB_REPO`, and cwd. The sole SQLite database
is the isolated `budget.sqlite3`; there are no service connections or migrations.
Daily output is `<shadow-root>/data/shadow/llm_extract_YYYYMMDD.jsonl`. It contains
validated evidence, hashes/version identities, frozen date/provenance, baseline,
usage, safe outcomes and ceiling cost; never full pages or raw model responses.

## Offline comparison and commands

Run from this checkout or another isolated checkout, with an external shadow root:

```sh
# Safe with the shipped config: returns disabled without fetching or creating state.
python3 -B scripts/llm_extract_shadow.py \
  --shadow-root /tmp/pitchboard-llm-shadow --url https://example.org/submissions

# Score only: no key lookup, sockets, corpus refresh, or execution of eval scripts.
python3 -B scripts/compare_llm_extract.py --eval-root /workspace/pb-eval \
  --shadow-root /tmp/pitchboard-llm-shadow

# Reuse any number of daily files for the one-week comparison.
python3 -B scripts/compare_llm_extract.py --eval-root /workspace/pb-eval \
  --shadow-root /tmp/pitchboard-llm-shadow \
  --predictions /tmp/pitchboard-llm-shadow/data/shadow/llm_extract_*.jsonl
```

An enabled, explicitly dated private config can later be supplied with `--config`;
`--operation generate --sample-size 112` uses only cached pages and freezes the
sample before inference. Both caps, window expiry and duplicate suppression apply
on resume. Turning the config off stops new work. There was no enabled run in BUILD.

Adapters select root heldout, d1030, B3 and d1102 heldout explicitly, using fixed
KST cohort dates 2026-10-29, 2026-10-30, and 2026-11-02. Manifest `sha` means
SHA-256(URL), never a body hash. Body/text hashes are computed locally. Frozen
split/manifest checksums and selected file hashes/mtimes are verified read-only.
Missing corpus is a reported exclusion, never a refetch. Known tune URL/body overlap
is excluded; cross-cohort URL/body overlap is retained in cohort reports and counted
once in the aggregate. This tool supports HTML only.

The B3 human-heading map expands the four translation-support IDs to 22 labeled
URLs, with seven writing positives and fifteen negatives; excluded labels do not
silently become negatives. Other historical before/after files are not gold.
The baseline mirrors the existing pure `promote_calls` replay: recruitment/rules
gate plus a future closing date or standing status, rather than merely checking
`open_call_eligible`. Reports include per-cohort/language denominators, abstentions,
real-call confusion matrices, deadline/pay set agreement, field coverage and raw
paired predictions. Eligibility region agreement is explicitly a rules projection
of the LLM quote, not independent annotation. Missing extraction/current-status
gold yields `accuracy=null`, `labeled=0`, `reason=missing_gold`.

## BUILD verification

`tests/test_llm_extract.py` and `tests/test_llm_compare.py` use temporary roots,
synthetic cohort copies and fake HTTP/provider responses. They cover all requested
guards, both thread/process budget contention, restarts, failures and UTC rollover,
key secrecy/file parsing, provider request policy, robots status handling, redirect
checks, hard Instagram denial, DB/promotion isolation, protected paths, exact
English/Korean evidence and offsets, frozen split/body integrity, tune exclusions,
deduplication, baseline mapping, gold/abstention metrics, and offline scoring.

The offline score smoke run read `/workspace/pb-eval` and wrote only beneath
`/tmp/pitchboard-llm-build-ki7h45n9/shadow`: 188 eligible HTML pages, 186 deduplicated,
zero model predictions. Exclusions were 183 access walls, 47 cached fetch denials,
27 oversized pages, 38 tune overlaps and 13 unsupported content types. Of the 22
B3 labels, 18 pass the access policy; the four translation-support pages are skipped
as access walls. This is a baseline/integrity check, not a measured LLM result.
Selected input hashes and mtimes were checked again before the report was written.

Final validation commands and results:

* `PYTHONPATH=src python3 -B -m pytest -q tests/test_llm_extract.py tests/test_llm_compare.py`:
  46 passed, 89 subtests passed.
* `PYTHONPATH=src python3 -B -m pytest -q`: 148 passed, 89 subtests passed,
  three failures in existing image/scanned-PDF OCR and collector E2E tests.
  Tesseract is absent locally; `.github/workflows/test.yml` installs it in CI.
  Running the original commit's collector tests from an untouched temporary
  `git archive HEAD` reproduces precisely the same three failures (16 passed).
  Attempts to install the CI OCR packages failed because the Debian package
  mirror connection is unavailable in this environment. No test was skipped,
  weakened or changed to conceal the failures.
* The shipped live-shadow CLI returned `disabled` without creating its root.
* `git diff --check` passed. No push, merge, real API generation or activation.

The original design below is retained as historical context. Its larger testing
matrix and planned refinements are design targets, not claims that every listed
scenario or later collector integration has been implemented. The BUILD contract
above describes actual behavior and explicitly supersedes settlement, repair,
configuration, eligibility schema and lifecycle details where they differ.

---

## Preserved DESIGN proposal (historical reference)

# Optional DeepSeek Flash extraction: shadow design

Status: DESIGN only, 2026-11-05; review pass 2026-11-05. Target branch: `feat/deepseek-extract-shadow`; never push to `main`.

This stage changes only this document. All modules, configuration, commands, tests, and runs described below are future implementation work. No API request, credential read, crawler execution, deployment, or evaluation run is part of this stage. The observed live runtime is PID 18143 (`bash /workspace/pitchboard/crawler/daemon.sh`, cwd `/workspace/pitchboard/crawler`), the main repository worktree `/workspace/pitchboard-data` (branch `p5-ontology-daemon`), and live data under `/workspace/pitchboard/data-root`. The current checkout, `/workspace/pitchboard-data-main`, is a linked worktree, not a runtime root. PID 18143, those paths, `/workspace/pb-eval`, databases, locks, and cron must remain untouched throughout this experiment.

## Outcome and isolation boundary

For an eligible fetched page, optionally ask DeepSeek Flash whether it describes a real writing-related open call and extract deadline, pay, and eligibility. Retain a field only with a nonempty verbatim quote found in that page's parsed text. Missing or invalid evidence makes the entire field `null`, including its value. Results are comparison data only, never facts for publication or promotion.

The first implementation runs as a separate, bounded shadow command in an isolated checkout/runtime directory, never in `/workspace/pitchboard-data-main` merely because this worktree exists. It reuses pure parsing and rules functions and a policy-enforcing fetch adapter. It does not run the normal collector CLI, open either live database, import an existing crawler's runtime configuration, or use its queues, caches, lock files, logs, or schedules. No migrations or new tables in existing databases. All new writable state is under an explicit, dedicated shadow root, for example `/workspace/pitchboard-llm-shadow`; it must not overlap `/workspace/pb-eval`, `/workspace/pitchboard-data` (main worktree), `/workspace/pitchboard-data-main` (this linked worktree), `/workspace/pitchboard` (crawler/live root), or `/workspace/pitchboard/data-root`. Resolve every candidate with `realpath` and reject when either path is a prefix of the other (`commonpath`), when any component is a symlink, or when a target is a hard link to an existing external file. Do not infer a root from `PB_DATA_ROOT`, `PB_REPO`, `git rev-parse --show-toplevel`, or the current working directory. Require an explicit `--shadow-root` created for this experiment (mode `0700`); reject an existing non-empty root unless it carries the experiment identity.

Within that root, outputs are `data/shadow/llm_extract_YYYYMMDD.jsonl`, budget state, isolated fetch state, and reports. No full page bodies are retained; the repository's existing policy permits short excerpts and hashes. Runtime artifacts and evidence must not be committed. The new code must not receive service DB handles or call promotion/store/scheduler APIs.

An eventual collector integration point is immediately after a successful HTML parse, alongside the rules calculation. It passes immutable page data to the same shadow interface and never consumes the LLM result in collector decisions. That integration is deferred until after the comparison and is not installed into PID 18143's checkout. A failure or budget stop in the optional step never changes rules output.

## Repository findings

* `collect/executor.py::fetch_page` parses pages, extracts and labels them, and writes through `store`; `collect/collector.py` promotes facts through `promote.promote_raw` and updates scheduler/budget infrastructure state. Neither is a safe shadow runner entry point. `promote_raw` uses `now[:10]` as its `today`; the eval replay's `promote_calls` helper in `/workspace/pb-eval/d1102/run.py` mirrors that call-creation decision and is the classification comparison target (see below). Do not substitute `collect/metrics.py::compute`'s `open_calls_current`: it is a live service-DB aggregate over stored facts, not a per-page predictor and not available under the shadow boundary.
* `collect/parse.py::Doc.text()` is exactly `"\n".join(s.text for s in doc.segments)`. `parse_html` decodes entities, collapses whitespace inside each segment, and (because the parser buffers `<title>` text before the first block segment) normally emits the HTML title as segment 0, followed by headings and body text. `doc.title`, `meta`, and JSON-LD are separate fields. Use `Doc.text()` byte-for-byte; do not prepend `doc.title` again and do not synthesize URL or metadata into evidence.
* Version strings to freeze: `PARSER_VERSION = "parse-1"`, `EXTRACTOR_VERSION = "pb-rules-5"`, `LABELER_VERSION = "pb-labels-2"`, and `EXECUTOR_VERSION = "exec-1+pb-rules-5+parse-1"`.
* The baseline is `profiles/pitchboard.py::extract` plus `label_doc`, with fixed `today` and source-authority inputs. Preserve rules versions, full predictions, and excerpts. `open_call_eligible` represents current eligibility, while a recruitment label can describe an expired call. `label_doc` defaults `source_official=True`; existing replays rely on that default, so record the assumption rather than inferring official status.
* `collect/net.py::Fetcher` checks robots per redirect and recognizes HTTP denials, challenges, and login walls, but writes host state to its supplied infrastructure connection. Use only isolated shadow fetch state. `_check_url` performs DNS resolution via `_addr_ok` before any block-list check, and it allows `PITCHBOARD_EGRESS_GATEWAY_CIDRS` (default `198.18.0.0/15`) without verifying the resolved address; the shadow wrapper must deny hosts before DNS and must not inherit that gateway exception unless the isolated runtime pins it explicitly. Its URL check does not apply `config/blocked_hosts.txt`; higher-level checks alone do not protect redirect targets. In `_robots_for`, any status other than 200/401/403/5xx (including 3xx and 429) currently parses as an empty rule set, which `robotparser` treats as allow; the shadow wrapper must replace this with the explicit table below.
* `store.is_blocked` matches both the literal host and `registered_domain(host)` against `config/blocked_hosts.txt`; the list includes Instagram and other social domains. The shadow boundary must additionally hard-deny `instagram.com` and all subdomains independently of this editable list.
* `scripts/devpipe/run_stage.sh` sources `~/.config/pitchboard/env` (observed: one `export DEEPSEEK_API_KEY=...` line, mode `0600`) and routes the dev-lane model through `https://api.deepseek.com/anthropic` with `ANTHROPIC_AUTH_TOKEN`. The shadow extractor must not source that file, must not reuse that wrapper or base URL, and must verify which DeepSeek transport/header/model id it is actually calling before implementation.
* The project uses the standard library and `unittest`. The proposal can use `urllib.request`, `json`, `decimal`, `sqlite3`, and `fcntl` without an SDK dependency.

## Proposed configuration and credentials

Future `config/llm_extract.json` defaults:

```json
{
  "enabled": false,
  "mode": "shadow",
  "model": "deepseek-flash",
  "daily_call_cap": 50,
  "daily_cost_cap_usd": "5.00",
  "budget_timezone": "UTC",
  "max_page_utf8_bytes": 24000,
  "max_output_tokens": 2048,
  "timeout_seconds": 30,
  "automatic_retries": 0,
  "window_start_utc": null,
  "window_end_utc": null,
  "pricing": {
    "input_usd_per_million_ceiling": "0.30",
    "output_usd_per_million_ceiling": "1.20",
    "input_token_reservation_ceiling": 1000000,
    "verified_on": "2026-11-05",
    "valid_until_utc": "2026-11-12T00:00:00Z"
  }
}
```

Missing configuration means disabled. Invalid types, unknown top-level keys, negative/nonfinite caps, unknown modes/models, expired pricing, or invalid dates disable extraction with a fixed reason code. Zero in either cap permits zero requests. Only literal JSON `true` enables it; a string `"true"`, a command-line flag, or an environment variable cannot override `false`. An enabled run requires explicit timezone-aware start/end timestamps with `start < end` and duration at most seven days; `pricing.valid_until_utc` must be at or after `window_end_utc`, otherwise the run is disabled with `pricing_expires_during_window`. Check configuration, window, and caps before every dispatch. Disabled mode returns before key lookup, page fetching for this feature, state creation, or network access.

`timeout_seconds` bounds only the model provider call. Page and `robots.txt` fetches use a fixed 15-second transport timeout in `policy.py`; raising `timeout_seconds` does not extend them, and lowering it does not shorten them. Cohort eligibility reads `max_page_utf8_bytes` through the same config parser as the runner (`common.page_limit`), so a lowered limit cannot silently diverge between the scorer and the extractor.

Resolve `DEEPSEEK_API_KEY` from the process environment first; otherwise parse only that assignment from `~/.config/pitchboard/env`. Do not source or execute the file or expand `~`, variables, command substitutions, or globs. Require a regular file owned by the current user with mode `0600` (reject group/world permission bits), reject symlinks, and accept at most one matching line. The observed file uses an `export DEEPSEEK_API_KEY=...` line, so accept an optional literal `export ` prefix; accept an unquoted token or a quoted value only when it contains no `$`, backtick, newline, or other shell metacharacter. Reject malformed, duplicate, or concatenated assignments without echoing the line. An explicitly empty environment variable is an error, not permission to search elsewhere. Never pass the key in argv (it is visible in process listings); send it only in the fixed endpoint's authorization header. Missing credentials skip extraction without prompting or spending; validate credential availability before reserving budget so a missing key does not consume a call.

Never log the key, authorization headers, request objects, prompts, page text, environment contents, or provider response/error bodies. Use allowlisted reason codes and safe numeric metadata; log prompt/page hashes and byte counts instead. Credential values must not be included in object representations, tracebacks, JSONL, reports, prompts, or fixtures; catch transport errors and emit `type(e).__name__` plus an allowlisted reason rather than formatting the exception or request object. The HTTP client uses default TLS verification, ignores `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` (install `urllib.request.ProxyHandler({})` unless an explicit, reviewed proxy is configured), connects only to the fixed HTTPS host on port 443, sends the documented authorization header, and refuses redirects, preventing credential forwarding. Verify whether the chosen DeepSeek endpoint expects `Authorization: Bearer` or the Anthropic-compatible token header; the dev-lane wrapper is not a specification.

## Fetch and access policy

The shadow extractor accepts only successful HTML pages with a verified policy envelope: requested URL, redirect chain, final URL, fetch timestamp, status, robots decisions, and parser version. PDF, image/OCR, feeds, empty shells, and unsupported content are explicitly out of scope for week one and counted as skips.

For any new fetch, apply these checks before DNS, robots requests, page requests, and each redirect hop:

1. Canonicalize before any resolution: lowercase, IDNA, strip one trailing dot, reject empty labels, userinfo, non-default ports, non-HTTP(S), percent/backslash/control characters in the host, and malformed or overlong hosts. Hard-deny `instagram.com` and every subdomain with `host == "instagram.com" or host.endswith(".instagram.com")`. Apply the repository block list and public-network rules to the canonical host **before DNS**, robots, page requests, or any redirect hop. This cannot be removed by configuration. Do not visit Instagram robots.txt, submit cached Instagram pages to the model, or follow links to it. Ignore Instagram links without creating leads.
2. Apply the same hostname denial and permission rules to the requested host and every redirect target. No alternate identities, proxies, cookies, login, mirror, alternate URL scheme, browser rendering, provider Search/Fetch tools, or gateway-CIDR allowance to evade a refusal.
3. Fetch robots using the collector identity (`PitchboardBot/0.1...`) and the isolated throttle state. Key caches by `(origin, user agent)` with a bounded TTL; never reuse crawler caches. Week one does not follow robots redirects. Use this table:

   | robots response | Decision |
   | --- | --- |
   | 200 with valid `text/plain` under the size cap | Parse; allow only if the exact URL passes `can_fetch` for the collector UA. |
   | 404, 410 | No robots policy; allow the page path. |
   | 401, 403, 429 | Disallow. |
   | 3xx (including 301/302/307/308) | Disallow; do not follow. |
   | 5xx, timeout, TLS/DNS error, empty/oversize/non-text/invalid response | Disallow. |
   | Any other status | Disallow. |

4. Honor page 401/402/403/429/451, login walls, CAPTCHA/challenges, and any other access denial as terminal for this attempt. Only a successful 2xx HTML response with usable parsed text can reach the model. No render fallback. Validate redirected targets before making any request to them. Reject responses over the transport byte cap, non-HTML content types, and compressed/decoded bodies that exceed the cap.

The existing fetcher cannot be assumed sufficient unchanged. A future isolated policy wrapper/subclass must enforce hostname denial at `_check_url` before DNS and implement the conservative robots status policy. Test it at the transport boundary. Recheck the envelope at extraction entry so callers cannot accidentally bypass the pre-fetch guard with cached data.

Offline evaluation does not fetch pages or robots. It skips blocked/Instagram URLs, denied outcomes, and cached HTML containing access walls. Record offline provenance separately: cached manifests do not prove a complete redirect/robots history. Do not label offline replay as a newly policy-verified live fetch or use it to fetch a denied resource. Missing corpus bytes are a skip, never a reason to refetch.

## Model request and evidence contract

Use a single fixed endpoint and transport, verified before implementation. The proposal is `POST https://api.deepseek.com/chat/completions` with `model=deepseek-flash`, non-streaming JSON output, explicit non-thinking mode, `temperature=0` (or the provider's deterministic setting), `top_p=1`, a bounded output limit, and no tools. The repository's dev lane uses the Anthropic-compatible `https://api.deepseek.com/anthropic` with `ANTHROPIC_AUTH_TOKEN`; that is a different contract, so the shadow client must not inherit the dev-lane base URL, headers, or environment. The official [pricing/model page](https://api-docs.deepseek.com/quick_start/pricing/) is unverified in this design pass; reverify the exact model id/alias, context window, tokenizer, peak/off-peak rates, and whether Flash bills reasoning tokens before activation. Record the requested model and any returned model/version/system fingerprint; treat an alias as untrusted and halt if the returned identity is not in an explicit allowlist. Never silently substitute another model. Request parameters must be checked against the [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/).

The [JSON output guide](https://api-docs.deepseek.com/guides/json_mode/) requires `response_format={"type":"json_object"}` and a JSON instruction/example in the prompt. JSON output does not replace local schema or evidence validation. Require a JSON `application/json` response under a fixed byte cap, parse with duplicate-key detection (`object_pairs_hook`), and reject unknown top-level or nested keys. Blank content, invalid JSON, duplicate keys, unexpected types/keys, tool calls, refusal, `finish_reason` other than a normal stop, missing/malformed `usage`, or truncation produce no accepted prediction and no automatic repair call.

Send a versioned system instruction and the page text as untrusted data, with `as_of` supplied separately. The prompt must distinguish author compensation from entry fees/prizes, calls from results/news/jobs/venue hire, and historical calls from currently open calls. It instructs the model to ignore instructions inside page content, use no external knowledge or linked-page evidence, and return `null` for unsupported or ambiguous values. Never send gold labels, baseline predictions, credentials, or filesystem paths. Use a JSON-escaped payload with a separate page-text field; delimiters are not treated as a security boundary.

Evidence text is exactly `doc.text()` from the full parsed page, i.e. `"\n".join(s.text for s in doc.segments)`. `parse_html` normally includes the `<title>` text as the first segment; do not prepend `doc.title` again, and do not synthesize URL or metadata into the evidence string. Skip pages whose `len(doc.text().encode("utf-8"))` exceeds `max_page_utf8_bytes` instead of truncating or making multiple requests; this makes missing evidence and comparisons unambiguous. Quotes must fit within a single segment, have 1–280 characters, and match case, whitespace, punctuation, and Unicode exactly. No fuzzy matching, translation, ellipsis replacement, quote normalization, or search across other pages. Compute the segment index and `[start, end)` character offsets locally against the segment text and verify `segment.text[start:end] == quote`; do not trust model offsets. If the quote occurs more than once, choose the first occurrence by `(segment_idx, start)` and record `occurrence_count`; if the occurrences have different surrounding context, record `ambiguous_occurrence`. Reject a quote spanning segment boundaries with `cross_segment_quote`. Apply the repository's V-10 hygiene rule: if the quote contains an email address or phone number, null the entire field with `evidence_contact_details` rather than storing the contact details or redacting the quote (redaction would break verbatim matching).

Illustrative accepted prediction (all four top-level fields are always present):

```json
{
  "open_call": {
    "value": {"is_real_call": true, "status": "open"},
    "quote": "Submissions are open until December 1, 2026."
  },
  "deadline": {
    "value": {"text": "December 1, 2026", "date": "2026-12-01", "timezone": null},
    "quote": "Submissions are open until December 1, 2026."
  },
  "pay": null,
  "eligibility": null
}
```

Each field is either `null` or exactly `{value, quote}` before local offsets are attached. Invalid evidence nulls the entire associated field, with a separate `validation_reasons` entry. A missing quote never leaves a surviving normalized value. Classification also requires evidence; lack of a call cue alone is an abstention, not an evidence-backed negative. If a page presents multiple distinct calls and the model cannot identify one unambiguous target, keep `open_call` if the page as a whole is a call page but null every call-specific field with `ambiguous_multiple_calls`; do not average or choose silently.

* `open_call.value`: `is_real_call` boolean and `status` in `open|closed|cancelled|upcoming|unknown|not_call`. A closed historical writing call can be real, but is not current. Contradictory combinations are invalid. Current-positive means real and explicitly open as of the fixed date; uncertain timing abstains. Pure results announcements and non-writing recruitment are not calls for Pitchboard.
* `deadline.value`: original short `text`, optional ISO date, optional explicitly stated timezone. Do not infer a year or timezone, choose between conflicting deadlines, or silently collapse multiple calls on an index page. For a range, use the closing end only when the quote marks it as the close. A supported ambiguous date may retain text with `date=null`; ambiguous attribution makes the entire field null. Rolling calls can have no deadline.
* `pay.value`: `text`, `state` (`paid|unpaid|conditional|unknown`), nullable currency, minimum/maximum decimal amounts, and unit. Normalize only what the quote supports; unknown does not mean unpaid. `conditional` means the quote makes payment contingent (for example "paid if selected" or "honorarium TBD"); `unknown` means the page does not state it. Amounts are JSON strings parsed with `Decimal` (never binary floats), and each amount must carry the currency/unit the quote supports. Submission fees and prize pools must not become compensation rates.
* `eligibility.value`: concise source-supported `text` plus a list of explicit constraints. Use a controlled vocabulary for constraint types (`region`, `residency`, `citizenship`, `age`, `language`, `experience`, `student_status`, `membership`, `other`) so constraints compare deterministically. Every constraint must be supported by the single quote; omit unsupported constraints. If a complete statement cannot fit, abstain and report evidence-length limitation rather than fabricate completeness.

Classification truth table: `not_call` requires `is_real_call=false` and nulls all call-specific fields; `open`, `closed`, `cancelled`, and `upcoming` require `is_real_call=true`; `unknown` may have `is_real_call=true` when the page is a real call but its timing is not explicit. Contradictory pairs (for example `not_call` with `is_real_call=true`, or `open` with a closed/past-deadline quote) are invalid. `open` requires an explicit current-open cue consistent with the supplied `as_of`; an upcoming or undated call abstains from current-positive rather than guessing. Define the classification unit explicitly: "does this page yield at least one real, currently open writing call under the collector's `promote_calls` semantics?" An index page that clearly yields one call is positive; an index page with several unrelated calls is positive only for the existence question and abstains on deadline/pay/eligibility.

Exact quote occurrence prevents invented citations, but does not prove the quote entails the value. Apply deterministic date/type/range/consistency checks, retain ambiguity, and measure semantic correctness with independent labels/review. Never promote a matching quote to verified truth automatically.

## Hard daily budgets

Both limits apply jointly to live-shadow and eval-generation requests using this feature. One dedicated `budget.sqlite3` under the shadow root owns all runs; callers cannot choose a fresh ledger per run. Multiple hosts require one shared coordinator or remain unsupported. Score-only runs make no API calls.

Use UTC dates and decimal USD arithmetic, rounded upward to a fixed precision. Validate credential availability before reserving so a missing key does not consume an attempt; never read the key value into a log or persisted object. Before dispatch, in a durable `BEGIN IMMEDIATE` transaction, check `attempts + 1 <= daily_call_cap` and `settled_cost + outstanding_reservations + worst_case_cost <= daily_cost_cap_usd`. Reserve one attempt and maximum cost, commit, then dispatch. Equality permits the reserved call; subsequent calls stop at either cap. No network request occurs if reservation or persistence fails. Recheck the day immediately before dispatch; a reservation crossing midnight must be moved atomically to the new day before sending. Record and retain the dispatch day through settlement.

For the first version, conservatively reserve the full verified model input-context ceiling (1,000,000 tokens) at the maximum input rate, plus `max_output_tokens` at the maximum output rate. With the illustrative rates this is USD 0.3024576 per request. **This makes the nominal 50-call cap unreachable:** `floor(5.00 / 0.3024576) = 16` dispatches/day, or at most 112 over the seven-day window. The held-out pool alone is about 450 usable pages (root 72, d1030 103, b3 40, d1102 235 before dedup), so this reservation can cover at most a quarter of one pass. Before an enabled run, choose and document one of: (a) a proven byte-based token bound — for a byte-level BPE tokenizer, input tokens cannot exceed UTF-8 bytes, so cap the entire request body (system + schema + page + framing) at, say, 64 KiB and reserve `body_bytes + margin` input tokens; with the illustrative rates that is about USD 0.022/request and makes the call cap bind; (b) an explicit eval-stage daily cost cap (for example USD 20) that covers the deterministic sample; or (c) a deterministic stratified sample small enough for 112 calls, with the unsampled pages reported as missing, not scored. Option (a) is preferred only if the provider documents the tokenizer as byte-level and non-thinking billing is verified; add a +10%/+256-token safety margin and re-verify against a real usage response before trusting it. Enforce the page-byte limit separately. Do not silently keep the 1M-token reservation while expecting broad coverage. Non-thinking mode prevents an unbudgeted reasoning allocation; verify provider accounting before activation.

The 24 KB page cap excludes 12/210 root-cohort, 13/103 d1030, 0/40 b3, and 39/232 d1102 parsed pages in the observed corpora. If option (a) is adopted, raise `max_page_utf8_bytes` to at least 64 KiB (roughly p90 for d1030 and d1102) and report the excluded tail; otherwise report oversized pages as missing rather than negatives. The reservation can be tightened only with a proven bound for tokenization, prompt framing, and all billed token classes.

After a response, settle valid usage at configured ceiling rates without cache/off-peak discounts; release unused reservation only after durable settlement. Missing/malformed usage, timeouts, crashes, and uncertain HTTP failures keep the full reservation charged. Count every dispatched attempt, including invalid JSON and failed requests. Never reclaim an uncertain request merely because its lease expired. Unexpected usage above the reservation or pricing/model mismatch halts all further requests and is a budget invariant failure. No autonomous retries, including SDK retries; a later explicit retry requires a fresh reservation.

Persist attempt IDs and states so restarts cannot double-release reservations or reset counters. Lock contention, corrupt/missing ledger after initialization, unwritable output/state, or clock moving backwards fails closed. Initial creation is permitted only for a fresh experiment, with an experiment identity outside individual run IDs; deleting/resetting state is not a resume mechanism. Caps are feature-wide, not account-wide: unrelated use of the same key cannot be controlled by this ledger. Price ceilings must be reverified before their validity expires; this is required for the stated cost bound.

## Shadow record and lifecycle

Each JSONL row includes schema version, experiment/run/attempt IDs, UTC timestamp and budget day, requested/final URLs, safe policy outcome, body SHA-256 (computed locally), text hash, parser/rules/prompt/schema/config hashes, requested and returned model identity, `as_of` with its source/timezone, baseline raw predictions and the `promote_calls` projection, validated LLM fields with `(segment_idx, start, end, quote_sha256, occurrence_count)`, validation reasons, outcome/skip reason, latency, usage, reserved cost, and charged ceiling cost. Never include headers, keys, full page text, prompts, raw provider responses, or malformed model output. Rule excerpts remain marked as rules data; report their own quote validity separately.

Use `umask 077`, create directories `0700` and files `0600`, and take an `fcntl.flock` process lock on a non-symlink lock file inside the shadow root. Append/flush/fsync bounded JSONL records; generate filenames from UTC dates, never from page/model text. Preflight writable state/output before reserving a call. Recovery tolerates only a partial trailing line, preserves budget reservations, and deduplicates by attempt ID. Keep a small durable outcome journal in shadow state with explicit states (`reserved`, `dispatched`, `responded`, `published`, `unknown`) so a crash between response handling and JSONL append can reconstruct the validated row without another call; the journal stores only validated fields, usage, and reason codes, never raw provider output or page text. If recovery is impossible, record an unknown outcome and retain its cost.

Deduplicate by experiment, canonical final URL, body hash, fixed `as_of` date, prompt/model/config versions, and provenance mode. This avoids repeat billing for identical page revisions while allowing explicit daily current-status comparisons. Keep the two modes separate: eval generation uses a frozen per-cohort `as_of` and dedups across the whole run; live shadow intentionally re-asks on a later day when current status can change. Retries never assume provider idempotency. Unexpected errors produce safe reason codes and leave baseline predictions usable.

The seven-day interval starts only when explicitly configured in a future run. Freeze code, prompt, model settings, sample-selection order, and scoring protocol before held-out inference. Use a deterministic sample of permitted pages, including rule negatives, rather than only rule-positive pages. Record all exclusions and cap-induced missing predictions. End timestamps automatically stop dispatch; turning `enabled=false` stops new work, with any already dispatched request remaining accounted for. No auto-enablement, cron installation, live-data backfill, or automatic production promotion at the end.

## Read-only held-out comparison

Future `scripts/compare_llm_extract.py` has two explicit operations: generation (requires enabled config and the shared caps) and score-only (default, offline, no key required). Outputs always go beneath the dedicated shadow root. It reads `/workspace/pb-eval` as data only, with bytecode writes disabled. Do not execute its existing scripts: some write beside their inputs or replay database operations.

Observed input contracts (verified read-only on 2026-11-05):

| Source | Use and limitations |
| --- | --- |
| Root `manifest.json`, `split.json`, `split_frozen.sha256`, `corpus/<sha>.bin` | `manifest.json` maps URL to outcome/status/content type/final URL/fetch timestamp and a `sha` field that is `sha256(url)`, **not** a body hash; `corpus/<sha>.bin` is named by that URL hash. `split.json` has 80 heldout and 141 tune URLs; after `outcome=ok` and corpus presence, about 72 heldout and 138 tune pages exist, of which 69 and 136 parse as HTML/PDF. `split_frozen.sha256` covers `split.json` and `manifest.json` only. Existing replay uses `today=2026-10-29`, the KST calendar date of the 2026-10-28T23:07Z fetch. |
| `d1030/manifest.json`, `manifest_b3.json`, `d1030/corpus/` | Frozen 2026-10-30 cohort (fetch timestamps 2026-10-29T23:0xZ, KST 10-30): 106 URLs (103 ok; groups `A_new_raw`, `B2_kr_board_detail`, `B_kr_expand`) and 40 B3 URLs (all ok, group `B3_kr_board_detail_final_heldout`). Replay `today=2026-10-30`. `fetch_fresh.sha256` hashes fetch/expand scripts, not corpus bodies (and lists two different `expand2.py` hashes), so compute body hashes locally. |
| `d1030/b3_gold.json` | Human writing/non-writing labels for **22** B3 pages that had a `deadline.closes_on` before the change: 7 writing (including one "ltikorea bbsIdx x4" aggregate) and 15 non-writing; ambiguous cases count as writing. It is keyed by human `cid=`/`bbsIdx=` headings, not URLs; build and freeze a deterministic label-to-URL mapping (expanding the x4 group) under shadow reports and verify it by hand. The other 18 B3 pages are unlabeled, not negatives. Supports only the writing/non-writing slice, not current-open accuracy or deadline/pay/eligibility accuracy. |
| `d1102/manifest_ho.json`, `corpus_ho/` | Held-out cohort fetched 2026-11-02 (timestamps 2026-11-01T23:0xZ, KST 11-02): 270 URLs, 235 ok, 232 parsed by the cohort's own README. Replay `today=2026-11-02`. `manifest_ho.sha256` hashes only the manifest, not bodies. |
| `d1101/manifest2.json`, `corpus2/` | Tune/replay material fetched 2026-10-31/11-01 (128 URLs), not held-out gold. Do not add it to held-out denominators. |
| `d1104/*`, `d1105/*` | Prior engineering tasks (projection/attachment and URL canonicalization), not extraction gold. `d1105` includes export-identity tests. Do not recursively ingest every dated directory as a new cohort. |
| Root and dated `heldout_before/after.json` | Historical rule/replay predictions, not independent gold. Use them only to reproduce the baseline, never as labels. |

Use an explicit cohort manifest in the future implementation rather than recursively treating every dated file as new gold. Derive `as_of` from each cohort's documented KST calendar date (root 2026-10-29, d1030/b3 2026-10-30, d1102 2026-11-02), record the derivation, and never use the implementation day. Hash and record every selected manifest/split/body file and the baseline commit; because manifest `sha` is a URL hash, compute a SHA-256 of each body at read time and use it for integrity and deduplication. Preserve original cohort results and a deduplicated aggregate. Observed cross-cohort overlap is small but real: 6 shared URLs between d1030 and d1102, 4 between b3 and d1102, and 3 identical bodies between root and d1102; assign a primary cohort deterministically (earliest frozen cohort first), report every overlap, and never double-count in aggregate metrics. Exclude tune leakage by URL and body hash. Fail closed on frozen-split mismatch, invalid `sha` (`^[0-9a-f]{64}$`), path traversal, symlink escape, or unsupported schemas. Missing or newly changed files invalidate that cohort; never modify or repair the eval tree, and never refetch a missing body. Input corpus paths must resolve inside the approved eval root.

Recompute the current branch's rules on the same parsed `Doc`, date, and authority as the LLM input using pure functions. For historical compatibility, existing replay defaults `source_official=true`; record this assumption rather than infer official status from an LLM. Mirror the existing `promote_calls` projection from `/workspace/pb-eval/d1102/run.py` in tested shadow code (do not import or execute eval scripts); it is the baseline for "this page yields an open call": a page qualifies when the recruitment/rules gate passes and it has at least one `deadline.closes_on >= as_of`, or a `status.declared=standing` value. Compare that to the LLM's current-positive classification, not to `any(l.open_call_eligible)`. Save raw baseline outputs, then map fields without changing rules:

| Comparison field | Rules projection | LLM value | Match rule |
| --- | --- | --- | --- |
| Classification (current-positive) | `promote_calls` non-empty | `open_call.value.is_real_call == true` and `status == "open"` | Exact boolean; abstention counts as a miss in end-to-end recall and separately in coverage. |
| Call exists (real/not current) | recruitment/rules label present | `is_real_call == true` regardless of timing | Exact boolean; do not conflate with current-open. |
| Deadline | Set of `deadline.closes_on.value.date` | `deadline.value.date` or null | Exact ISO date; retain sets and report ambiguity; null/text-only is not a date match; score only pages with an annotated date. |
| Pay state | `compensation.rate` `value_state` (`stated`/`explicitly_none`) | `pay.value.state` | `explicitly_none` ↔ `unpaid`, `stated` with amount ↔ `paid`, `conditional` is its own class, absent ↔ `unknown`; unknown never counts as unpaid. |
| Pay amount | `amount_min`/`amount_max`/`currency`/`unit` | same fields | Exact `Decimal` + currency + unit; report amount and state matches separately. |
| Eligibility region | `eligibility.region` (`scope`, `regions`, `excluded`, `basis`, `korea_resident_eligible`) | `eligibility.value.constraints` | Compare canonical region codes/basis/exclusions exactly; free text only by independent adjudication. |
| Eligibility statement | `eligibility.statement.value.text` | `eligibility.value.text` | Score only when independently annotated; otherwise report coverage/agreement, not accuracy. |

Multiple rule values are retained as sets and ambiguity is explicit. Do not silently drop extra predictions to improve scores. Score only explicitly annotated fields; missing annotation is not a negative.

Report per cohort and language:

* Independent gold: confusion matrix, precision/recall/F1 for real-call classification and separately current-open status where actually labeled. The b3 gold labels only writing/non-writing; it does not label current-open, so do not infer current-open gold from `heldout_before/after` predictions. Report abstention counts/coverage and end-to-end recall with positive abstentions counted as misses, alongside covered-subset scores.
* Labeled extraction: exact normalized deadline match, structured pay match (amount/currency/unit/state), and adjudicated eligibility constraints; give precision, recall, coverage, and denominators. Score only explicitly annotated fields; missing annotation is not a negative. Track null-on-absent accuracy separately from extraction recall.
* Evidence: valid exact-quote rate for each retained field, invalid proposals nulled, offset/occurrence correctness, contact-detail nulls, and manually reviewed semantic-support errors. Accepted LLM evidence validity must be 100% by construction.
* Without independent labels: paired LLM/rules agreement, disagreements, null rates, and field coverage, clearly marked as agreement rather than accuracy. Report the complete eligible cohort and paired subset separately; cap/network/parse skips never disappear from denominators.
* Cost/operations: attempts, charged upper-bound USD, cost per accepted result, latency distribution, duplicate suppression, access/Instagram skips, and failure reasons.
* Power/disclosure: access-wall screening can leave very few gold positives. In the observed B3 cohort it leaves 3 writing positives and 15 negatives (all four ltikorea translation-support pages are currently treated as walls). Week-1 label metrics are low-power and must repeat this caveat. A human, at run time, must adjudicate whether those public board pages are genuine access walls; the question is not resolved in code.

Where gold is absent, accuracy is `null` with `missing_gold` and labeled count zero. A future blind annotation file may be created only under shadow reports, keyed by cohort/URL/text hash/date; reviewers see source evidence without model identity. Freeze annotations independently and report their scope. Do not tune on the held-out results. The existing synthetic `tests/gold` remains a regression suite, not new independent held-out evidence. Freeze the generation sample before any enabled call: sort eligible pages by `sha256(experiment_id + "\0" + canonical_url + "\0" + body_sha256)`, exclude non-ok/blocked/Instagram/oversized/access-walled pages, stratify by cohort and language, and record every exclusion. The sample size must fit the daily/weekly budget after the reservation decision above; report unsampled pages as missing rather than silently dropping them. Score-only mode reuses the frozen JSONL and makes zero network calls even if `DEEPSEEK_API_KEY` is set.

Example future invocation shape (not run in DESIGN): `python3 -B scripts/compare_llm_extract.py --eval-root /workspace/pb-eval --operation score --predictions <shadow-jsonl> --shadow-root /workspace/pitchboard-llm-shadow`. Generation uses an explicit `--operation generate` and the same disabled-by-default config; it never refreshes corpus pages. Run with `-B`, `PYTHONDONTWRITEBYTECODE=1`, an isolated `PYTHONPYCACHEPREFIX`/`TMPDIR` under the shadow root, `umask 077`, and the shadow root as cwd. Score-only mode must not import the credential loader or open a socket, and it writes only `comparison.json` in the validated shadow root: it does not create `experiment.json`, a lock, or the budget ledger. Before and after a run, verify that the eval tree has no new files and no changed mtimes for the selected manifests/splits/corpus files; hash the selected inputs and baseline commit into the report.

First enabled run: use a fresh shadow root and `--operation generate --sample-size 1`. The frozen sample is immutable per root, so do not reuse that root for the full run. This single URL validates the live model identity/parameters before the sample loop. A returned model outside the allowlist (`deepseek-flash`, `DeepSeek-V4.1-Flash-1017`) durably halts the experiment; conversely, provider 4xx/5xx failures are charged but do not halt, so a bad key would consume the daily cap in failed attempts — the smoke test makes that unlikely. Recheck pricing before enabling and keep the shipped config disabled until the authorized run.

## Required implementation tests

Tests use fake HTTP/provider transports, synthetic pages, temporary roots, and an injected UTC clock. No real key, external HTTP, live DB, PID, cron, or `/workspace/pb-eval` mutation is needed. Test every guard through the public shadow entry point as well as targeted helpers.

| Guard | Required assertions |
| --- | --- |
| Disabled by default | Missing/false/string-`"true"` config, unknown config keys, invalid config, zero caps, inactive/expired window, and pricing that expires before the window ends: zero provider calls; disabled path does not read secrets or create files. A fixture collector baseline remains identical with the feature disabled or failing. |
| Daily call cap | N requests allowed, N+1 denied; failed and malformed attempts count; restart and concurrent processes cannot exceed N; UTC rollover resets only the new day; midnight dispatch and backwards clock handled. Assert the configured call cap also holds when the cost cap is the binding constraint and when the byte-bound reservation is used. |
| Daily cost cap | Reserve before sending; exact boundary, insufficient remaining funds, Decimal rounding, concurrency, all usage categories, missing usage, timeout, crash-before/after-send, double settlement, stale rates, corrupt state, and output failure cannot allow overspend or refund uncertain cost. Both caps enforced independently and together; the 1M-token reservation and the byte-bound reservation each produce the documented effective cap, and pricing validity is checked against the window. |
| Secret handling | Environment precedence, `export DEEPSEEK_API_KEY=...` fallback parsing, safe permissions/symlink rejection, missing/empty key, duplicate/malformed/shell-looking contents never executed, key never in argv. A sentinel key appears only in the mocked auth header and never in captured stdout/stderr/logs/errors/artifacts; `HTTP(S)_PROXY`/`ALL_PROXY` are ignored; provider redirect is refused. |
| Robots/access | Disallow blocks page and model calls; 200 rules, 404/410, 401/403/429, 3xx, 5xx, timeout, malformed/oversize/non-text robots, and per-origin caching. Host denial runs before DNS; `PITCHBOARD_EGRESS_GATEWAY_CIDRS` cannot be used to reach a denied host. Page denials/login/challenge never invoke LLM, renderer, mirror, or fallback transport. |
| Instagram/block list | Direct, subdomain, uppercase, trailing-dot, ports, userinfo, percent/backslash/control-character tricks, redirects, cached/eval pages, canonical hints and links. No DNS/robots/page/model request for a blocked target; boundary hostname matching does not confuse `notinstagram.com`. Removing Instagram from the file cannot remove its hard denial. |
| Shadow only | Production DB connectors and store/promotion/scheduler functions replaced with sentinels that raise on use. Exercise success, failures, scoring, and recovery with `PB_DATA_ROOT`, `PB_REPO`, and cwd pointing at decoys. Assert all writes stay inside shadow/temp roots; reject symlink/hard-link/path escapes and all overlaps with the observed live/eval roots via `realpath`/`commonpath`. |
| Quote contract | Valid English/Korean evidence; title-as-segment-0 behavior; empty/missing/fabricated/translated/normalized/overlength quotes; cross-page and cross-segment quotes; repeated occurrences; email/phone-containing quotes; Unicode/punctuation mismatch; Decimal amount strings; invalid field value; partial field failure. Entire unsupported field becomes null; accepted offsets reproduce the exact quote and occurrence metadata. |
| Classification/extraction | Real vs current/closed, results/news/non-writing notices, unknown period, cancellations, multiple calls and index pages, missing year/timezone, unpaid vs absent/conditional, fee/prize vs pay, eligibility contradictions, and prompt-injection text. The truth table and the mirrored `promote_calls` comparison are exercised. No external tool use or linked-page lookup. |
| Provider robustness | Blank content, malformed/duplicate-key JSON, extra keys, wrong types, truncation/`finish_reason`, refusal, oversized/wrong-content-type response, missing/malformed usage, auth failure/429/5xx, timeout, model mismatch. Fixed reasons only, zero automatic retries, costs retained. |
| Persistence/week | Daily rotation, deterministic dedup, competing appenders, partial last line, journal-state crash recovery, seven-day expiry, and disabled-on-resume. Exactly one published row per completed attempt; no lost budget charge. |
| Evaluation integrity | Fixture clones of observed schemas; `sha` interpreted as `sha256(url)`, not body hash; locally computed body hashes; frozen split checks; b3's 22-label mapping (including the x4 expansion); tune overlap exclusion; cohort dedup/overlap; KST-derived fixed dates; correct `promote_calls` and field mapping; no refetch; gold vs prediction distinction; missing-gold null scores; hand-computed metric/abstention cases. Hash fixture inputs before/after and trap all attempted writes. |
| Operational isolation | Entrypoints reject the observed live/eval roots (`/workspace/pitchboard-data`, `/workspace/pitchboard-data-main`, `/workspace/pitchboard`, `/workspace/pitchboard/data-root`, `/workspace/pb-eval`) and do not launch crawler, change cron, signal processes, or invoke DB migrations. Verify with mocks/sentinels, never by probing or modifying PID 18143. |

Future verification: run the focused new tests and existing collector/profile regression suite in the isolated implementation checkout with temporary roots. Do not claim any of these tests exist or passed at DESIGN stage.

## Review criteria and implementation sequence

1. Implement the pure schema/evidence validator, config/key loader, conservative access adapter, durable budget accounting, and file-only sink with guard tests. Keep the checked-in flag false. Resolve the reservation decision (1M-token vs proven byte bound vs explicit eval cap) before writing the ledger, because the ledger schema and effective cap depend on it.
2. Implement the standalone shadow runner and read-only comparison adapters. Mirror `promote_calls`, build and freeze the b3 label mapping, and record body hashes. Confirm the full regression suite and all guard tests without real calls. Freeze the experiment protocol, sample order, prompt/schema hashes, and pricing before any enabled run.
3. In a later authorized run, perform bounded held-out generation and the seven-day shadow comparison. Keep PID 18143 and its files/cron unchanged. Report labeled accuracy where available, coverage, disagreements, evidence errors, and cost; document sample bias, unsampled pages, and absent labels.
4. Finish with a review artifact and the flag disabled. Acceptance requires zero access/Instagram violations, zero live writes, zero key leaks, no cap violation, and 100% exact evidence validity among accepted fields. Quality improvements require independently labeled denominators; agreement alone does not justify production use. Any later live integration is separate work, with no automatic merge, push to main, or promotion.

## Review pass notes (2026-11-05)

Verified in this pass and corrected above:

* Manifest `sha` is `sha256(url)` and names the corpus file; it is not a body hash. Frozen hash files cover manifests/scripts only. Body integrity must be computed by the shadow implementation.
* The b3 human label set is 22 pages (7 writing, 15 non-writing), not 19, and its labels are human headings needing a frozen URL mapping. The other 18 B3 pages are unlabeled.
* `Doc.text()` already includes the HTML title as segment 0; it is not just body prose. Evidence/offsets must target that exact string and must not prepend the title again.
* The illustrative 1M-token reservation yields 16 calls/day under the $5 cap, so the 50-call cap is unreachable and one week covers at most 112 calls against a ~450-page held-out pool. The document now requires an explicit budget/sample decision.
* The env file uses an `export DEEPSEEK_API_KEY=...` line at mode `0600`; the dev lane uses a different (Anthropic-compatible) DeepSeek transport. The parser and transport sections now reflect both.
* The robots/status table and DNS-before-block ordering close gaps in the existing `Fetcher`; exact-quote evidence now has a deterministic occurrence rule and a V-10 contact-detail handling rule.
