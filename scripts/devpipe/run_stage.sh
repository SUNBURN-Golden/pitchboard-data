#!/usr/bin/env bash
# Usage: run_stage.sh <stage> <logfile> <prompt>
# Tries each runner in config/dev_lanes.json[<stage>] in order until one succeeds. Secrets never logged.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
[ -f "$HOME/.config/pitchboard/env" ] && . "$HOME/.config/pitchboard/env"   # cron: DEEPSEEK_API_KEY (chmod 600)
stage="$1"; log="$2"; prompt="$3"
mapfile -t lanes < <(python3 -c 'import json,sys;print("\n".join(json.load(open(sys.argv[1]))[sys.argv[2]]))' "$HERE/config/dev_lanes.json" "$stage") || exit 2
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
    glm)      printf "%s\n" "$prompt" | env -u CLAUDE_CONFIG_DIR claude -p --dangerously-skip-permissions --effort xhigh ;;
    devin|swe2) local pf; pf="$(mktemp)"; printf "%s\n" "$prompt" > "$pf"
              devin -p --prompt-file "$pf" --model "$model" --permission-mode dangerous --respect-workspace-trust false; local rc=$?; rm -f "$pf"; return $rc ;;
    *) echo "unknown runner $1"; return 2 ;;
  esac
}
for lane in "${lanes[@]}"; do
  echo "[$(date -Is)] $stage -> $lane" >> "$log"
  if run_one "$lane" >> "$log" 2>&1; then echo "[$(date -Is)] $stage OK via $lane" >> "$log"; exit 0; fi
  echo "[fallback] $lane failed (rc=$?)" >> "$log"
done
[ -n "${DEEPSEEK_API_KEY:-}" ] && sed -i "s|$DEEPSEEK_API_KEY|***|g" "$log"
exit 1
