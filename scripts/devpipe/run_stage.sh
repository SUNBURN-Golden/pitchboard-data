#!/usr/bin/env bash
# Usage: run_stage.sh <stage> <logfile> <prompt>
# Tries each runner in config/dev_lanes.json[<stage>] in order until one succeeds. Secrets never logged.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
[ -f "$HOME/.config/pitchboard/env" ] && . "$HOME/.config/pitchboard/env"   # cron: DEEPSEEK_API_KEY (chmod 600)
stage="$1"; log="$2"; prompt="$3"
LANES_JSON="${PB_DEV_LANES:-$HERE/config/dev_lanes.json}"
mapfile -t lanes < <(python3 -c 'import json,sys;print("\n".join(json.load(open(sys.argv[1]))[sys.argv[2]]))' "$LANES_JSON" "$stage") || exit 2
: > "$log"
run_one() {
  local kind="${1%%:*}" model="${1#*:}"
  case "$kind" in
    codex)    printf "%s\n" "$prompt" | codex exec --dangerously-bypass-approvals-and-sandbox -m "$model" -c model_reasoning_effort='"high"' - ;;
    deepseek) [ -n "${DEEPSEEK_API_KEY:-}" ] || { echo "DEEPSEEK_API_KEY not set"; return 77; }
              mkdir -p "$HOME/.claude-deepseek"
              printf "%s\n" "$prompt" | env -u CLAUDE_CONFIG_DIR CLAUDE_CONFIG_DIR="$HOME/.claude-deepseek" \
                ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic ANTHROPIC_AUTH_TOKEN="$DEEPSEEK_API_KEY" \
                ANTHROPIC_MODEL="$model" ANTHROPIC_SMALL_FAST_MODEL="$model" ANTHROPIC_DEFAULT_HAIKU_MODEL="$model" API_TIMEOUT_MS=600000 \
                claude -p --dangerously-skip-permissions --model "$model" ;;
    qwen)     local qwen_env="${QWEN_ENV:-/workspace/qwen_env.sh}"
              [ -f "$qwen_env" ] || { echo "qwen env missing"; return 77; }
              (
                set +u
                # shellcheck disable=SC1090
                . "$qwen_env"
                printf "%s\n" "$prompt" | env \
                  ANTHROPIC_MODEL="$model" \
                  ANTHROPIC_DEFAULT_OPUS_MODEL="$model" \
                  ANTHROPIC_DEFAULT_SONNET_MODEL="$model" \
                  ANTHROPIC_DEFAULT_HAIKU_MODEL="$model" \
                  ANTHROPIC_SMALL_FAST_MODEL="$model" \
                  API_TIMEOUT_MS=600000 \
                  claude -p --dangerously-skip-permissions --model "$model"
              ) ;;
    glm)      printf "%s\n" "$prompt" | env -u CLAUDE_CONFIG_DIR claude -p --dangerously-skip-permissions --effort xhigh ;;
    devin|swe2) local pf; pf="$(mktemp)"; printf "%s\n" "$prompt" > "$pf"
              devin -p --prompt-file "$pf" --model "$model" --permission-mode dangerous --respect-workspace-trust false; local rc=$?; rm -f "$pf"; return $rc ;;
    *) echo "unknown runner $1"; return 2 ;;
  esac
}
redact_log() {
  [ -n "${DEEPSEEK_API_KEY:-}" ] && sed -i "s|$DEEPSEEK_API_KEY|***|g" "$log"
  python3 -c '
import pathlib, sys
log = pathlib.Path(sys.argv[1])
key_path = pathlib.Path("/home/box/.qwen/token_plan_key")
if not log.is_file() or not key_path.is_file():
    raise SystemExit(0)
key = key_path.read_text().strip()
if key:
    log.write_text(log.read_text().replace(key, "***"))
' "$log"
}
for lane in "${lanes[@]}"; do
  echo "[$(date -Is)] $stage -> $lane" >> "$log"
  run_one "$lane" >> "$log" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then echo "[$(date -Is)] $stage OK via $lane" >> "$log"; redact_log; exit 0; fi
  echo "[fallback] $lane failed (rc=$rc)" >> "$log"
done
redact_log
exit 1
