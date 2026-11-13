#!/usr/bin/env bash
# Daily Pitchboard collector run (cron). Budgeted; data root outside the repo; never touches the live crawler DB/daemon.
set -euo pipefail
REPO=${PB_REPO:-/workspace/pitchboard-data-main}
ROOT=${PB_DATA_ROOT:-/workspace/pitchboard/data-root}
CAPS=${PB_CAPS:-'{"discovery":{"requests":300},"backfill":{"requests":300},"freshness":{"requests":80}}'}
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
timeout 3h python3 -u -m pbdata.collect.cli run --root "$ROOT" --run-tag "d$TAG" --run-id "run_d$TAG" --api-cap 120 --max-jobs 600 --caps "$CAPS" >> "$LOG" 2>&1 || echo "run exit $?" >> "$LOG"
timeout 1h python3 -u -m pbdata.collect.cli refresh --root "$ROOT" --run-tag "f$TAG" --max-jobs 80 --caps "$CAPS" >> "$LOG" 2>&1 || echo "refresh exit $?" >> "$LOG"
python3 -m pbdata.collect.cli metrics --root "$ROOT" > "$ROOT/exports/metrics_$TAG.json" 2>> "$LOG"
