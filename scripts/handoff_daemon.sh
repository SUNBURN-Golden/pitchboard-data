#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
REPO=${PB_REPO:-/workspace/pitchboard-data-main}
ROOT=${PB_DATA_ROOT:-/workspace/pitchboard/data-root}
export PYTHONPATH="$REPO/src"
root_arg=false
dry_run=false
no_deploy=false
for arg in "$@"; do
  if $root_arg; then
    ROOT=$arg
    root_arg=false
    continue
  fi
  case "$arg" in
    --root) root_arg=true ;;
    --root=*) ROOT=${arg#--root=} ;;
    --dry-run) dry_run=true ;;
    --no-deploy) no_deploy=true ;;
  esac
done
if $dry_run; then
  exec python3 -u -m pbdata.handoff.daemon "$@"
fi
mkdir -p "$ROOT/logs" "$ROOT/locks"
exec >>"$ROOT/logs/handoff_daemon.log" 2>&1
exec 9>>"$ROOT/locks/handoff_daemon.lock"
flock -n 9 || { echo "$(date -Is) handoff daemon already running"; exit 0; }
if ! $no_deploy && ! python3 -c 'import blake3' >/dev/null 2>&1; then
  echo "$(date -Is) Cloudflare Pages deployment requires the Python blake3 module"
fi
exec python3 -u -c 'from pbdata.handoff.daemon import main; raise SystemExit(main(lock_held=True))' "$@"
