# Ontology daemon (P5)

`PYTHONPATH=src python3 -m pbdata.daemon run|status|questions|retry --root <data-root>`

Stages (each checkpointed in `infra.stage_run`, keyed by run_id + input digest):
discovery → incremental_fetch (manual clips + refresh + budgeted collector) → change_detection → extraction (rules only; AI path off, model cost cap 0) → entity_resolution (merge *proposals* only) → evidence_verification → state_update → service_recompute (`pb_search_projection`, tombstones, outbox) → quality_check (`metric_snapshot` label `daemon_quality`).

`daemon_quality.sources_never_succeeded` counts `allowed_public` / `allowed_conditional` sources with no `pb_raw_ref` whose `outcome='ok'`. Raw history is authoritative even when a historical `last_success_at` is NULL, so the metric corrects itself at the next quality check without a backfill. `insert_raw` records the latest successful raw's `fetched_at` in `pb_source.last_success_at` in the same transaction; failed or empty outcomes leave it unchanged.

Safeguards: `locks/daemon.lock` + `locks/collector.lock` (flock), `locks/KILL_SWITCH`, 3 bounded attempts with backoff, then `failure_queue` and stop at `failed_at:<stage>`; `run --run-id <id>` resumes from the failed stage. Crash test: `PB_DAEMON_CRASH_AT=<stage>`.

Rules: newer `status.declared` evidence moves a call to cancelled/closed/reopened (with history in `pb_status_event`); the clock may only close a call after the latest possible deadline instant and may never cancel. Missing conditions are `unknown`, never "no restriction". Call-level facts override outlet-level ones; conflicting values are kept and shown as `conflict`.

Scheduling: cron `30 5 * * * scripts/daemon_daily.sh` (renewal of public approvals due in 48h, then one run), after the 04:20 collector `daily.sh`.

## Dev pipeline lanes (준태님 2026-11-03 12:55 KST)
Stage model order lives in `config/dev_lanes.json` (primary first, then backups); `scripts/devpipe/run_stage.sh <stage> <log> <prompt>` tries them in order.
design: Codex sol 6.2 xhigh → Qwen4-Max → DeepSeek flash → GLM → swe-2 · design review: DeepSeek flash → Qwen4-Max → Codex sol 6.2 xhigh → GLM → swe-2 ·
build: Codex sol 6.2 xhigh → Qwen4-Max → DeepSeek flash → GLM → swe-2 · build review: GLM → Qwen4-Max → DeepSeek flash → Codex sol 6.2 xhigh → swe-2 ·
review fix: DeepSeek flash → Qwen4-Max → Codex sol 6.2 xhigh → GLM → swe-2 · re-review: Devin automatic (adaptive) → Qwen4-Max → DeepSeek flash → Codex sol 6.2 xhigh → swe-2 ·
audit every 10 PRs vs the original blueprint: Codex sol 6.2 xhigh → Qwen4-Max → GLM → swe-2.
DeepSeek runs via Claude CLI on `https://api.deepseek.com/anthropic` (model `deepseek-flash`). Cron reads `DEEPSEEK_API_KEY` from `~/.config/pitchboard/env` (600, not in git).
Qwen4-Max (`qwen4-max-preview`) is the first backup on every stage, via Claude CLI after sourcing `/workspace/qwen_env.sh` (key at `/home/box/.qwen/token_plan_key`, never logged or committed). Credit, rate-limit, quota, or auth failures fall through to the next lane.
