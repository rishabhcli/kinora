#!/usr/bin/env bash
# Load full agent mission into .claude/ralph-loop.local.md (for Ralph stop-hook re-feed).
#
# Writes to the Claude Code session directory (cwd or CLAUDE_PROJECT_DIR), NOT always repo root.
# Run from the same directory where you started `claude` (usually your worktree).
#
# Usage:
#   cd ../kinora-a01 && bash ../kinora/agent-prompts/arm-ralph.sh 01
#   cd agent-prompts && bash arm-ralph.sh 01   # if Claude workspace is agent-prompts/

set -euo pipefail

AGENT="${1:?usage: arm-ralph.sh 01-12}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PROMPTS="$SCRIPT_DIR"
TARGET_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

case "$AGENT" in
  01) FILE="agent-01-event-director-stitching.md"; MAX=500; PROMISE="AGENT 01 COMPLETE" ;;
  02) FILE="agent-02-scroll-film-engine.md"; MAX=500; PROMISE="AGENT 02 COMPLETE" ;;
  03) FILE="agent-03-film-api-sync.md"; MAX=500; PROMISE="AGENT 03 COMPLETE" ;;
  04) FILE="agent-04-motion-animation.md"; MAX=500; PROMISE="AGENT 04 COMPLETE" ;;
  05) FILE="agent-05-library-books-epub.md"; MAX=500; PROMISE="AGENT 05 COMPLETE" ;;
  06) FILE="agent-06-accessibility.md"; MAX=500; PROMISE="AGENT 06 COMPLETE" ;;
  07) FILE="agent-07-optimization.md"; MAX=500; PROMISE="AGENT 07 COMPLETE" ;;
  08) FILE="agent-08-color-depth-typography.md"; MAX=500; PROMISE="AGENT 08 COMPLETE" ;;
  09) FILE="agent-09-settings-sf-symbols.md"; MAX=500; PROMISE="AGENT 09 COMPLETE" ;;
  10) FILE="agent-10-book-open-film-experience.md"; MAX=500; PROMISE="AGENT 10 COMPLETE" ;;
  11) FILE="agent-11-login-experience.md"; MAX=500; PROMISE="AGENT 11 COMPLETE" ;;
  12) FILE="agent-12-integration-captain.md"; MAX=500; PROMISE="AGENT 12 COMPLETE" ;;
  *) echo "unknown agent: $AGENT" >&2; exit 1 ;;
esac

BODY="$PROMPTS/.missions/$FILE"
[[ -f "$BODY" ]] || { echo "missing $BODY" >&2; exit 1; }

BYTES="$(wc -c < "$BODY" | tr -d ' ')"
if [[ "$BYTES" -lt 1000 ]]; then
  echo "ERROR: body file suspiciously small ($BYTES bytes) — aborting" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR/.claude"
STATE="$TARGET_DIR/.claude/ralph-loop.local.md"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
  echo "---"
  echo "active: true"
  echo "iteration: 1"
  echo "session_id:"
  echo "max_iterations: $MAX"
  echo "completion_promise: \"$PROMISE\""
  echo "started_at: \"$STARTED_AT\""
  echo "agent: \"$AGENT\""
  echo "prompt_file: \"agent-prompts/.missions/$FILE\""
  echo "---"
  echo ""
  cat "$BODY"
} > "$STATE"

echo "armed Agent $AGENT -> $STATE ($BYTES bytes)"
if [[ "$BYTES" -lt 5000 ]]; then
  echo "WARN: body smaller than expected; check for truncation" >&2
fi
