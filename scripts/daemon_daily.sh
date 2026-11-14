#!/usr/bin/env bash
# Daily: renew public_document approvals due within 48h (=> each URL renewed on its 7-day cycle), then one daemon run.
set -euo pipefail
cd "${PB_REPO:-/workspace/pitchboard-data-main}"
git pull --ff-only -q || true
ROOT=/workspace/pitchboard/data-root
mkdir -p "$ROOT/logs"
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
export PITCHBOARD_SOCKS5_PROXY="${PITCHBOARD_SOCKS5_PROXY:-socks5h://127.0.0.1:1080}"
export PITCHBOARD_PROXY_TRAFFIC_LOG="${PITCHBOARD_PROXY_TRAFFIC_LOG:-$ROOT/logs/proxy_traffic.log}"
if ! python3 -m pbdata.collect.egress wait; then
  echo "socks5 proxy unavailable; skipping daemon $(date)" >>"$ROOT/logs/daemon.log"
  exit 0
fi
# The 04:20 collector can run past 05:30; wait (up to 3h) for it to release collector.lock instead of failing.
# collector.lock is released briefly between daily.sh's run and refresh steps, so first wait for the
# whole daily.sh (it holds daily.lock for its entire run), then for collector.lock.
flock -w 10800 "$ROOT/locks/daily.lock" true || echo "daily.lock still held after 3h $(date)" >>"$ROOT/logs/daemon.log"
flock -w 10800 "$ROOT/locks/collector.lock" true || echo "collector.lock still held after 3h $(date)" >>"$ROOT/logs/daemon.log"
python3 scripts/renew_public_document.py --horizon 48 >>"$ROOT/logs/renewal.log" 2>&1 || echo "renewal failed $(date)" >>"$ROOT/logs/renewal.log"
PYTHONPATH=src python3 -m pbdata.daemon run --root "$ROOT" --max-jobs 600 \
  --caps '{"discovery":{"requests":300},"backfill":{"requests":300},"freshness":{"requests":80}}' >>"$ROOT/logs/daemon.log" 2>&1
