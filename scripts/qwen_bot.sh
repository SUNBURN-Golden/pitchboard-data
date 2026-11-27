#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
REPO=${PB_REPO:-/workspace/pitchboard-data-main}
ROOT=${PB_DATA_ROOT:-/workspace/pitchboard/data-root}
export PYTHONPATH="$REPO/src"
export PITCHBOARD_SOCKS5_PROXY="${PITCHBOARD_SOCKS5_PROXY:-socks5h://127.0.0.1:1080}"
export PITCHBOARD_PROXY_TRAFFIC_LOG="${PITCHBOARD_PROXY_TRAFFIC_LOG:-$ROOT/logs/proxy_traffic.log}"
mkdir -p "$ROOT/logs" "$ROOT/locks"
exec >>"$ROOT/logs/qwen_bot.log" 2>&1
exec 9>>"$ROOT/locks/qwen_bot.lock"
flock -n 9 || { echo "$(date -Is) qwen bot already running"; exit 0; }
exec python3 -u -c 'from pbdata.handoff.bot import main; raise SystemExit(main(lock_held=True))' --root "$ROOT" "$@"
