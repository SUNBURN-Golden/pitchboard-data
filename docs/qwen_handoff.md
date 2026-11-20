# Pitchboard Qwen handoff

The collection bot reads a temporary SQLite backup of the service DB. It
selects active official sources and sources with live call deadlines, then
fetches through Pitchboard's SOCKS5 egress and robots policy. It runs the
deterministic rules and sends sanitized page text to Qwen for a second opinion.
A published batch contains the page records and a checksummed manifest. The
daemon re-runs the rules on the stored text, applies the verified results to the
service DB, updates its projection, and rebuilds the site from a committed Git
tree with the evidence-backed overlay.

Qwen uses the existing token-plan key loader. The Qwen API, Cloudflare Pages
API, and alerts dispatch use direct egress. Page fetches require the SOCKS5
proxy. No assistant tokens or assistant services are used at runtime. The site
fetch from GitHub is best effort and falls back to committed local main.

## Schedule

The box runs in Asia/Seoul. The bot runs at 11:00 and 18:00 KST. The handoff
daemon runs at 13:00 KST.

These are the proposed cron entries for the box operator.

```cron
# Pitchboard Qwen bot / handoff daemon (Qwen only; no assistant tokens)
0 11,18 * * * PATH=/usr/local/bin:/usr/bin:/bin /workspace/pitchboard-data-main/scripts/qwen_bot.sh # pb-qwen-bot
0 13 * * * PATH=/usr/local/bin:/usr/bin:/bin /workspace/pitchboard-data-main/scripts/handoff_daemon.sh # pb-handoff-daemon
```

## Paths and controls

The wrappers default to `PB_REPO=/workspace/pitchboard-data-main` and
`PB_DATA_ROOT=/workspace/pitchboard/data-root`. Paths below are relative to
the data root unless specified.

| Path | Contents |
| --- | --- |
| `service_db/pitchboard_kg.sqlite3` | Service DB |
| `logs/qwen_bot.log`, `logs/handoff_daemon.log` | Run logs |
| `handoff/status.json` | Latest bot and daemon outcomes and cumulative batch counts |
| `handoff/qwen/usage.jsonl` | Bot usage per run, including skipped runs |
| `handoff/qwen/state/success.json` | Bot's recent successful fetches for the 20-hour cutoff |
| `handoff/qwen/inbox/` | Batches awaiting processing |
| `handoff/qwen/processed/`, `handoff/qwen/failed/` | Batches with a terminal ledger entry |
| `handoff/qwen/ledger.sqlite3` | One terminal state and outcome detail per batch |
| `handoff/qwen/review/<batch_id>.json` | Rules agreement by field and advisory Qwen values and evidence |
| `handoff/site_overlay.json` | Accepted source checks and verified call deadlines with evidence IDs |
| `exports/projection_latest.json` | Refreshed service search projection |
| `backups/pitchboard_kg.pre-qwen-handoff-*.sqlite3` | Up to 14 pre-write service backups |

The caps in `config/qwen_bot.json` include repair calls in the Qwen request
limit. The bot gives first priority to live deadlines within 21 days. A
failed SOCKS5 probe skips publication after five attempts. Fetch failures that
occur after a successful probe are included in the batch.

The handoff consumes at most ten batches, oldest first. Replay rechecks the
source registry and declines a stale observation if a newer successful raw
already exists. A capped page cannot withdraw deadlines based on unseen text.
The site publishes verified deadlines and only advances outlet check dates
when the outlet can be mapped by its official URLs. The build runs in a
temporary directory using `git archive` from `/workspace/pitchboard-site`.
Deployment requires the box's Python `blake3` module and runtime token at
`/home/box/.secrets/pitchboard_cf_pages_token.txt`. Alerts dispatch uses
`/home/box/.secrets/pitchboard_alerts_cron_secret` when present.

The daemon accepts `--root`, `--site-repo`, `--max-batches` (0–10),
`--dry-run`, and `--no-deploy`. Dry-run prints the pending batch IDs and
committed site revision using local state. With `--no-deploy`, the daemon
still runs the DB phase, builds and verifies the site, and dispatches alerts
when configured.

## Weekly share cards

The 13:00 KST handoff generates a Top 10 rates card on Tuesdays and a
deadlines card on Thursdays. It uses the same overlaid site data as the
verified build, including when deploy fails or `--no-deploy` is set. Use
`--cards auto|top10|deadlines|both|none` to control generation. Dry-runs
skip card generation. Each attempted card has a `kind`, `ok`, `out_dir`,
and `error` in the `last_daemon_run.cards` list in `handoff/status.json`.
Card errors leave the site and alerts phases independent.

Top 10 uses the directory’s `pay_usd_max` ordering and excludes sponsored
and featured listings. Cards show pay in its original currency. The
deadline window includes the current US Eastern date through seven days
later. Calls that have yet to open are skipped. The footer and output
folder use the KST date. A deadlines card requires at least one eligible call.

Each draft goes to
`/workspace/pitchboard/social/weekly-cards/YYYY-MM-DD/` with landscape and
portrait SVGs and PNGs, separate X, Threads, and Reddit copy, and a metadata
file with the source date, rows, and pre-posting checks. The Reddit draft
includes the full portrait list. Generation is local.

To generate a draft manually, point `--site-data` at a directory containing
the site JSON files. The supplied date sets the output date and the calendar
date at the start of the Eastern deadline window.

```bash
PYTHONPATH=src python3 -m pbdata.handoff.cards --card top10 --date 2026-11-17 \
  --site-data /path/to/site/data --out-root /path/to/weekly-cards
```

The standalone command also accepts `deadlines` and `auto`. It reuses
complete drafts for a date and kind unless `--force` is supplied. The
renderer uses CairoSVG via `/workspace/pitchboard/venvs/cards/bin/python`
or the interpreter set by `PB_CARDS_PYTHON`. Inter and Source Serif Pro
must be available through fontconfig.

## Locks and recovery

The wrappers use nonblocking exclusive locks on `locks/qwen_bot.lock` and
`locks/handoff_daemon.lock`. The bot waits up to 30 minutes for the daily and
collector locks and holds them through collection to reserve proxy access.
For its DB phase, the daemon waits up to 30 minutes for the bot lock, then up
to two hours for the daily, collector, and ontology daemon locks. The site
phase still runs when the DB phase is skipped, using the last saved overlay.
Updates to status are serialized by `handoff/status.lock`.

A batch directory is published by renaming it after its manifest and items
are synced. Staging directories and batches with a missing or mismatched
checksum are ignored. A service-side receipt is committed with the batch
transaction. If a run stops between that commit and the ledger update, the
next run recovers the receipt. A terminal ledger entry also prevents replay
if moving a directory fails.

To retry a failed batch, first diagnose the error in its review file and
ensure there is no running bot or daemon. Move the batch directory from
`handoff/qwen/failed/` back to `handoff/qwen/inbox/` and delete that batch's
failed row from `handoff/qwen/ledger.sqlite3`. A failed batch's service
transaction was rolled back. The next daemon run or dry-run will see the
batch again. For corrupt batches, restore the original items and manifest
before retrying.
