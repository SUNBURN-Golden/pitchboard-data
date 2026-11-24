#!/usr/bin/env bash
# Isolated DeepSeek shadow daily batch.
# Does not write the live service database or touch collector/daemon locks.
set -euo pipefail
umask 077

REPO=${PB_REPO:-/workspace/pitchboard-data-main}
ROOT=${PB_SHADOW_ROOT:-/workspace/pitchboard-llm-shadow}
CFG=${PB_SHADOW_CONFIG:-$HOME/.config/pitchboard/llm_extract.json}
OPS=${PB_SHADOW_OPS:-$HOME/.local/state/pitchboard-llm-shadow}
SERVICE_DB=${PB_SERVICE_DB:-/workspace/pitchboard/data-root/service_db/pitchboard_kg.sqlite3}
LIMIT=${PB_SHADOW_LIMIT:-40}
LOG=${PB_SHADOW_LOG:-$OPS/cron.log}

install -d -m 0700 "$OPS" "$OPS/tmp" "$OPS/pycache"
# Cron already holds $OPS/cron.lock around this script. Do not flock it again.
exec >>"$LOG" 2>&1
echo "$(date -Is) shadow daily start limit=$LIMIT"

export PITCHBOARD_SOCKS5_PROXY="${PITCHBOARD_SOCKS5_PROXY:-socks5h://127.0.0.1:1080}"
export PITCHBOARD_PROXY_TRAFFIC_LOG="${PITCHBOARD_PROXY_TRAFFIC_LOG:-$OPS/proxy_traffic.log}"
export PYTHONPATH=$REPO/src
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX=$OPS/pycache
export TMPDIR=$OPS/tmp

cd "$REPO"

if ! python3 -m pbdata.collect.egress wait; then
  echo "$(date -Is) socks5 proxy unavailable; skipping shadow run"
  exit 0
fi

GAPS=$OPS/gaps.json
if ! python3 -B "$REPO/scripts/llm_extract_shadow_export.py" \
    --service-db "$SERVICE_DB" --output "$GAPS" --tmp-dir "$OPS/tmp"; then
  echo "$(date -Is) gap export failed; stopping"
  exit 1
fi

set +e
python3 -B "$REPO/scripts/llm_extract_shadow_batch.py" \
  --config "$CFG" \
  --shadow-root "$ROOT" \
  --gaps-json "$GAPS" \
  --limit "$LIMIT"
status=$?
set -e
echo "$(date -Is) shadow daily end status=$status"
exit "$status"
