# Active collector (engine + Pitchboard profile)

The collector *finds* sources, *tries* them, *collects* from them, and *looks for what is missing*. It is not a fixed-URL revisit loop.
Everything lives in `src/pbdata/collect/` (shared engine) and `src/pbdata/profiles/pitchboard.py` (the only Pitchboard-specific file:
lexicons, extractors, labels, required fields). Another service reuses the engine with its own profile and target list.

**Promotion is rules-only.** Every extractor, label and gap decision is rule-based. A model may later be added for ambiguous
documents only (not implemented); until then ambiguous input yields an explicit unknown (`unknown_period`, no deadline), never a guess.
`model_cost_usd` in the metrics is therefore exactly 0.

## Flow

```
targets (country x language x genre x organizer type x year x event keywords)      config/targets/pitchboard.json
   |  discover  (Wikidata class x country/language SPARQL, Wikipedia category directories, entity-search query combos,
   |             entity follow-up from names found in text; relevance filters, exclusions and blocked hosts)
   v
pb_discovery_candidate  -- evaluate (trial crawl: relevance, novelty vs seed list / registered domain / entity duplicate,
   |                                 extractability, original vs repost/aggregator, robots+HTTP permission)
   |   registered | registered_existing | deprioritized | rejected | manual_lead (SNS: never fetched)
   v
registered site (outlet + site source)  -- expand_site (robots Sitemap / sitemap.xml / RSS, ranked by HIGH_VALUE_PATH, old-year URLs skipped)
   v
fetch_page  executor: API/feed -> HTML/structured -> browser render only for pages that need inline JS (never to bypass a denial)
   |   pagination (rel=next / ?page=N), tables, PDF text+tables (OCR if scanned), image notices (OCR), login wall / challenge / empty body = failure
   |   chaining: attachments, apply form, SNS -> manual lead, entity names -> follow-up discovery, same-site call/guideline links
   v
L1 pb_raw_ref   (url, content/text hash, fetched_at, fetch_method, outcome, change_state, extractor_version; NO body)
L2 pb_extraction (field, value, <=280-char sanitized excerpt + locator, extractor, confidence)
L3 pb_label     (recruitment | rules | correction | extension | cancellation | results | other; basis fact/claim/inference; status)
L4 pb_fact / pb_call_edition / pb_status_event   (append-only, evidence-backed, conflicts kept, see DESIGN.md)
   v
gap finder: not_extracted | absent_in_source | inaccessible | conflict   (never AI-filled)  -> gap_fill: same-site reverse search by field hints
```

Before queueing or registering discovered pages, the collector strips share and tracking parameters, preserves functional query parameters, and skips binary assets by extension. Guideline documents such as PDF, DOCX, and HWP remain eligible. The daemon records how many old junk sources it retires before fetching, keeping all rows and protecting site roots, manual clips, and sources with successful fetches, facts, or evidence. Both metrics exports report `sources_retired_junk` and `sources_never_fetched`. The latter counts active sources with neither a success timestamp nor a successful raw reference. Run `python scripts/eval_junk_sources.py SERVICE_SQLITE` to evaluate retirement and check protected sources on a temporary database copy.

For `policy.ai`, `mentioned_unclassified` is a non-committal mention and cannot contradict another source's stated policy for the same dimension; same-source edits still supersede, and classified disagreements still open conflicts. Before the daily service projection and quality check, the daemon resolves groups with no remaining active cross-source contradiction and records an automatic review decision. Facts and their historical conflict links stay intact, with resolved groups ignored by current projections and gap checks; affected calls are marked dirty. Run `python scripts/eval_conflict_resolution.py SERVICE_SQLITE` to report the effect and verify fact integrity on a temporary copy without writing the original database.

## Safety rules enforced in code
* robots.txt is checked per hop and cached 24 h; 401/403/5xx on robots = deny; unreachable robots = nothing fetched (class `error`, retry later).
* 429 halts the host for 6 h; repeated 5xx for 1 h; min 2 s between requests per host; max 4 redirects, each re-checked; SSRF guard.
* Denials, login walls and challenges are *recorded as failures* and never retried with other identities, proxies, headless tricks or captcha solving.
* The seven `access_review` domains and social hosts are `config/blocked_hosts.txt`: rejected before any request. Instagram/Facebook/X/Threads/TikTok
  links are stored only as `manual_lead` candidates (host + first path segment) and are never fetched.
* Last year's calls and award/results announcements are labelled `stale_prior_year` / `results_not_call` and never promoted to calls.
* OCR-derived facts are never `verified_fact` (max `source_claim`). Verified needs a deterministic re-parse of the stored excerpt on an official, non-OCR source.
* A changed deadline on a single-deadline page whose call has not closed is the *same* call (superseded value kept as history; later date => `extended`).

## Scheduler
Jobs live in the infra DB with an idempotency key (`kind|subject|run_tag`), 300 s leases, expired-lease requeue (resume), bounded retry with backoff
(3 attempts -> `dead` + `failure_queue`), a daily request ledger for each lane (`discovery`, `backfill`, `freshness`) and a flock `RunLock`.
`priority = 0.30 novelty + 0.25 gap importance + 0.20 urgency(deadline) + 0.15 quality - 0.10 cost - 0.15 duplication - 0.20 min(1, failures/3)`.
Checkpoints are written after every job. `PB_CRASH_AFTER_LEASE=n` kills the process while holding a lease (used for the resume proof).

Refresh prioritizes sources backing live deadline/status facts that are stale or expire within 48 hours (including confirmations), soonest expiry first. Past deadlines are excluded. Configure the horizon with `PB_REFRESH_HORIZON_HOURS`. Canonical URLs with successful HTML/PDF/image fetches are preferred, then other URLs refresh oldest first. Compare top-40 coverage with `python scripts/eval_refresh_live_calls.py SERVICE_SQLITE [--now YYYY-MM-DDTHH:MM:SSZ]`. It queries a temporary read-only copy and defaults to the latest raw fetch time.

## Caps removed 2026-11-20

The collector and ontology daemon accept optional `--max-jobs`, `--api-cap`,
`--refresh-limit` and `--caps` in their respective `cli.py` files. For example,
`--caps '{"discovery":{"requests":150}}'` enables a daily request cap for the
discovery lane. The scheduler checks it between jobs and accounts for requests
in each lane. Omitted lanes, omitted request limits, and JSON `null` are unlimited.

`src/pbdata/collect/cli.py` and `src/pbdata/daemon/cli.py` dropped the 90-call
Wikimedia API defaults. `src/pbdata/collect/collector.py` dropped the
150/200/60 discovery/backfill/freshness request defaults, the 10-page site
cap, the 3,000-URL sitemap limit, and the two-page and two-attempt gap-fill
defaults. Refresh enqueueing dropped its 30-job collector default in that
file and its 40-job daemon default in `src/pbdata/daemon/stages.py`.

Set optional count limits in the top-level `budget` object passed via
`--targets`. Omitted or JSON `null` values are unlimited.

| Source | Optional settings |
| --- | --- |
| `src/pbdata/collect/{collector,discover,wiki}.py` | `api_calls`, `max_candidates` per strategy, `max_depth` for discovery, `wikidata_results`, `category_members`, `max_queries`, `search_results`, `entity_search_results`, `call_index_pages` |
| `src/pbdata/collect/{collector,evaluate}.py` | `trial_pages`, `max_pages_per_site`, `crawl_depth`, `pagination_hops`, `pagination_links`, `follow_links`, `entity_followups`, `entity_followup_depth` |
| `src/pbdata/collect/collector.py` | `sitemap_files`, `sitemap_urls`, `feeds`, `feed_items`, `expansion_pages`, `gap_attempts`, `gap_pages`, `refresh_limit` |

`PB_MAX_PAGES_PER_SITE` overrides the site page setting. Each `call_indexes`
entry can set `max_pages` and `max_details`. The shipped target file omits these
limits. HTTP requests run serially with robots, proxy fail-closed behavior,
honest User-Agents, blocked hosts, permission checks, and access denials enforced.

## Alerts (extraction breakage)
`extraction_breakage` (>=2 core fields before, 0 after on a changed page), `field_lost`, `segments_collapsed` (<30% of >=8), `page_gone`, `login_wall`, `challenge`, `denied_*`.
Alerts do not delete or overwrite facts.

## Run
```
export PYTHONPATH=src
python -m pbdata.collect run     --root $PB_DATA_ROOT --run-tag $(date +%Y%m%dT%H%M%S)
python -m pbdata.collect refresh --root $PB_DATA_ROOT --run-tag f1
python -m pbdata.collect metrics --root $PB_DATA_ROOT                    # metrics scoped to config/targets/*.json
python -m pbdata.collect status  --root $PB_DATA_ROOT
```
Resume after a crash: run the same command again with the same `--run-tag` and `--run-id` and `--no-seed` (leases expire after 300 s and are re-queued).

`open_calls_list` contains at most 50 distinct `call_id` values, matching the identity used by
`open_calls_current`. Each entry retains all eligible `deadline_evidence` (deadline, epistemic, source).
The legacy summary fields show the earliest eligible deadline, then strongest evidence and URL order;
alternative deadlines remain evidence and are not resolved by the metric.

## Known limits (honest)
* Browser render = headless chromium `--dump-dom` over the already-fetched HTML with the network disabled (inline JS only). JS apps that need XHR stay `needs_render` failures.
* No general web SERP provider (interface only; scraping a search engine is not done). Discovery uses Wikidata/Wikipedia APIs and links found on registered sites.
* The sandbox cannot reach plain `http://` or some TLS hosts; those candidates end as `deprioritized: entry_unusable:error` and are not retried this run.
* Gold samples (`tests/gold/`) are hand-written by the author and the rules were corrected after the first run on them, so they overstate generalisation.
* Page-level sitemap/link lists are not persisted; gap-fill re-fetches the sitemap once per run per site.
* Deadline timezone is `unknown` unless the page states one; calls without a stated year are never given a date.
