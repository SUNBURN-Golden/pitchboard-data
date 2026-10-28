# Operations

## Layout (data never lives in this repo)
```
$PB_DATA_ROOT/            (outside the repo, chmod 700; e.g. /workspace/pitchboard/data-root)
  service_db/pitchboard_kg.sqlite3   service DB  (pb_*: facts, evidence, calls, raw refs, extractions, labels, gaps, candidates)
  infra_db/infra.sqlite3             infra DB    (job, budget_ledger, host_state, checkpoint, alert, runs, failure_queue, metric_snapshot)
  locks/  exports/  backups/  snapshots/  rawstore/
```
Raw page bodies are NOT stored: L1 keeps url + hash + fetched_at + extractor version, L2 keeps a <=280-char excerpt + locator (retention decision #2).
Migrations are additive and checksummed (`pbdata.db.migrate(conn, "service"|"infra")`; the CLI applies them on start).

## Coexistence with the live crawler
The running crawler daemon (`/workspace/pitchboard/crawler`, `inbox.sqlite3`) is not read, written or restarted by this code. `config/seed_hosts.txt`
is a static copy of its seed domains, used only to decide "novel vs already known". Renewing the crawler's `public_document` fetch approvals
(approved default #3) mutates its DB and has **not** been executed; do it deliberately and never for the 7 blocked domains.

## Daily operation
1. `run` (discovery + backfill + freshness budgets are separate; defaults 150/200/60 requests per day, override with `--caps`).
2. `refresh` re-fetches the oldest / most urgent pages; unchanged pages only log `unchanged`.
3. `metrics` -> JSON. Watch `alerts` (extraction_breakage, field_lost, segments_collapsed, page_gone, login_wall, challenge, denied_*) and `failure_queue`.
4. A crashed run: start the same command again (same `--run-tag`, `--run-id`, `--no-seed`). Leased jobs expire after 300 s and are re-queued; jobs that fail 3 times are dead-lettered.

## Gap handling
`not_extracted` (page had no parseable value; gap_fill tries same-site reverse search, max 2 attempts) -> `filled` (fact with evidence) or stays `open`;
`absent_in_source` (inspected pages state nothing; recorded as `not_stated_in_inspected_scope`, never as "no restriction"; stays open until reviewed);
`inaccessible` (login wall / robots / HTTP denial; `blocked`); `conflict` (sources disagree; both kept in a conflict group).

## Rules-only / cost
AI extraction is disabled (cap 0): no model calls, no per-fact model cost. OCR is local tesseract. Request cost = HTTP requests + Wikimedia API calls (`--api-cap`).

## Edition semantics
`edition_key` is the first-seen close date (or year) of a call. A later date on the same single-deadline page while the call is still live is the *same* call:
the old value is superseded (history kept) and the status becomes `extended`. A different page/year creates a new call edition. Calls are never closed by the clock alone.

## Social media
Instagram/Facebook/X/Threads/TikTok: no official-API access, never fetched. Links found on official pages are stored as `manual_lead` candidates; a human clips
the notice, and that clip enters as `source_claim`/`unverified` with its own source record.

## Manual clips (Instagram and other social)
No automatic fetching of social sites. A human drops `$ROOT/inbox/manual_clips/*.json` with `url, clipped_by ("human:<name>"), text, outlet_id|outlet_name, clipped_at, posted_at`. The daemon stores them as `source_claim` only; clips that do not name a known outlet go to `unresolved/`.

## public_document renewal
`scripts/renew_public_document.py [--dry-run] [--horizon 48]` renews 7-day public approvals only if robots allows our UA and an anonymous GET returns 200 without a login wall; it backs up the crawler DB first and never touches blocked hosts. Report: `exports/renewal_YYYYMMDD.json`.
