# Ontology daemon (P5)

`PYTHONPATH=src python3 -m pbdata.daemon run|status|questions|retry --root <data-root>`

Stages (each checkpointed in `infra.stage_run`, keyed by run_id + input digest):
discovery → incremental_fetch (manual clips + refresh + budgeted collector) → change_detection → extraction (rules only; AI path off, model cost cap 0) → entity_resolution (merge *proposals* only) → evidence_verification → state_update → service_recompute (`pb_search_projection`, tombstones, outbox) → quality_check (`metric_snapshot` label `daemon_quality`).

Safeguards: `locks/daemon.lock` + `locks/collector.lock` (flock), `locks/KILL_SWITCH`, 3 bounded attempts with backoff, then `failure_queue` and stop at `failed_at:<stage>`; `run --run-id <id>` resumes from the failed stage. Crash test: `PB_DAEMON_CRASH_AT=<stage>`.

Rules: newer `status.declared` evidence moves a call to cancelled/closed/reopened (with history in `pb_status_event`); the clock may only close a call after the latest possible deadline instant and may never cancel. Missing conditions are `unknown`, never "no restriction". Call-level facts override outlet-level ones; conflicting values are kept and shown as `conflict`.

Scheduling: cron `30 5 * * * scripts/daemon_daily.sh` (renewal of public approvals due in 48h, then one run), after the 04:20 collector `daily.sh`.
