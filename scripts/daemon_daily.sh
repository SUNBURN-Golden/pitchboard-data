#!/usr/bin/env bash
# Daily: renew public_document approvals due within 48h (=> each URL renewed on its 7-day cycle), then one daemon run.
set -euo pipefail
cd "${PB_REPO:-/workspace/pitchboard-data-main}"
git pull --ff-only -q || true
ROOT=/workspace/pitchboard/data-root
mkdir -p "$ROOT/logs"
python3 scripts/renew_public_document.py --horizon 48 >>"$ROOT/logs/renewal.log" 2>&1 || echo "renewal failed $(date)" >>"$ROOT/logs/renewal.log"
PYTHONPATH=src python3 -m pbdata.daemon run --root "$ROOT" --max-jobs 600 \
  --caps '{"discovery":{"requests":300},"backfill":{"requests":300},"freshness":{"requests":80}}' >>"$ROOT/logs/daemon.log" 2>&1
