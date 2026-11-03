# Ontology daemon (P5)

`PYTHONPATH=src python3 -m pbdata.daemon run|status|questions|retry --root <data-root>`

Stages (each checkpointed in `infra.stage_run`, keyed by run_id + input digest):
discovery → incremental_fetch (manual clips + refresh + budgeted collector) → change_detection → extraction (rules only; AI path off, model cost cap 0) → entity_resolution (merge *proposals* only) → evidence_verification → state_update → service_recompute (`pb_search_projection`, tombstones, outbox) → quality_check (`metric_snapshot` label `daemon_quality`).

Safeguards: `locks/daemon.lock` + `locks/collector.lock` (flock), `locks/KILL_SWITCH`, 3 bounded attempts with backoff, then `failure_queue` and stop at `failed_at:<stage>`; `run --run-id <id>` resumes from the failed stage. Crash test: `PB_DAEMON_CRASH_AT=<stage>`.

Rules: newer `status.declared` evidence moves a call to cancelled/closed/reopened (with history in `pb_status_event`); the clock may only close a call after the latest possible deadline instant and may never cancel. Missing conditions are `unknown`, never "no restriction". Call-level facts override outlet-level ones; conflicting values are kept and shown as `conflict`.

Projection inheritance is source-bound: an outlet/organizer fact applies to a call only when that exact source also has a live, evidence-backed `source_claim` or `verified_fact` on the call. Shared site ownership, the `official_guidelines` source type, and AI-inferred relations do not establish applicability. Separate guidelines pages need an explicit evidence-backed call fact before their outlet facts can be inherited; otherwise the condition stays unknown. Projection revision IDs include only evidence used by the selected facts. Stored facts and conflicts are preserved.

Scheduling: cron `30 5 * * * scripts/daemon_daily.sh` (renewal of public approvals due in 48h, then one run), after the 04:20 collector `daily.sh`.

## Dev pipeline lanes (준태님 2026-11-03 12:55 KST)
Stage model order lives in `config/dev_lanes.json` (primary first, then backups); `scripts/devpipe/run_stage.sh <stage> <log> <prompt>` tries them in order.
design: Codex astra 6 high → DeepSeek flash → GLM → swe-2 · design review: DeepSeek flash → Codex sol 6.1 high → GLM → swe-2 ·
build: Codex sol 6.1 high → DeepSeek flash → GLM → swe-2 · build review: GLM → DeepSeek flash → Codex sol 6.1 high → swe-2 ·
review fix: DeepSeek flash → Codex sol 6.1 high → GLM → swe-2 · re-review: Devin automatic (adaptive) → DeepSeek flash → Codex sol 6.1 high → swe-2 ·
audit every 10 PRs vs the original blueprint: Codex astra 6 high → GLM → swe-2.
DeepSeek runs via Claude CLI on `https://api.deepseek.com/anthropic` (model `deepseek-flash`). Cron reads `DEEPSEEK_API_KEY` from `~/.config/pitchboard/env` (600, not in git).
