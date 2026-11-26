#!/usr/bin/env bash
# Daily Pitchboard collector run (cron). Data root outside the repo.
set -euo pipefail
# The whole body is a function so bash parses it before running: the git pull below can rewrite this file
# mid-run, and bash reads scripts incrementally (2026-11-24: "bdata.collect.cli: command not found").
main() {
REPO=${PB_REPO:-/workspace/pitchboard-data-main}
ROOT=${PB_DATA_ROOT:-/workspace/pitchboard/data-root}
TAG=$(date +%Y%m%d)
LOG=$ROOT/exports/daily_$TAG.log
exec 9>"$ROOT/locks/daily.lock"
flock -n 9 || { echo "$(date -Is) another daily run holds the lock" >> "$LOG"; exit 0; }
cd "$REPO"
git pull -q --ff-only origin main >> "$LOG" 2>&1 || echo "$(date -Is) git pull failed; running current checkout" >> "$LOG"
export PYTHONPATH=$REPO/src
# Web fetches go through the box SOCKS5 (zecbot). Fail closed: never fall back to the box IP.
export PITCHBOARD_SOCKS5_PROXY="${PITCHBOARD_SOCKS5_PROXY:-socks5h://127.0.0.1:1080}"
export PITCHBOARD_PROXY_TRAFFIC_LOG="${PITCHBOARD_PROXY_TRAFFIC_LOG:-$ROOT/logs/proxy_traffic.log}"
mkdir -p "$ROOT/logs"
if ! python3 -m pbdata.collect.egress wait; then
  echo "$(date -Is) socks5 proxy unavailable; skipping collector run" >> "$LOG"
  exit 0
fi
# resume anything left by a crashed run of today (same tag/run-id), then refresh, then metrics
# --time-budget stops leasing new jobs before the hard timeout, so the run ends cleanly (locks released, queue kept)
# --lock-wait waits for a daemon still holding collector.lock (the daemon stops leasing 30 min before 04:20); the wait
# is not part of --time-budget, and the outer timeout covers wait + budget.
timeout -k 60 4h python3 -u -m pbdata.collect.cli run --root "$ROOT" --run-tag "d$TAG" --run-id "run_d$TAG" --time-budget 9900 --lock-wait 3600 >> "$LOG" 2>&1 || echo "run exit $?" >> "$LOG"
# refresh leases freshness jobs only (live-call pages first, then oldest fetch)
timeout -k 60 2h python3 -u -m pbdata.collect.cli refresh --root "$ROOT" --run-tag "f$TAG" --time-budget 3000 --lock-wait 3600 >> "$LOG" 2>&1 || echo "refresh exit $?" >> "$LOG"
python3 -m pbdata.collect.cli metrics --root "$ROOT" > "$ROOT/exports/metrics_$TAG.json" 2>> "$LOG"
}
main "$@"
exit
