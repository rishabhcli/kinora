#!/usr/bin/env bash
# One command before Claude Code — arms Ralph with the FULL mission (no truncation).
#
# Usage (from repo root):
#   bash agent-prompts/launch-agent.sh 01
#
# Then paste the one-liner from agent-prompts/agent-01-*.md into Claude Code.

set -euo pipefail

AGENT="${1:?usage: launch-agent.sh 01-12}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

case "$AGENT" in
  01) WT="../kinora-a01" ;;
  02) WT="../kinora-a02" ;;
  03) WT="../kinora-a03" ;;
  04) WT="../kinora-a04" ;;
  05) WT="../kinora-a05" ;;
  06) WT="../kinora-a06" ;;
  07) WT="../kinora-a07" ;;
  08) WT="../kinora-a08" ;;
  09) WT="../kinora-a09" ;;
  10) WT="../kinora-a10" ;;
  11) WT="../kinora-a11" ;;
  12) WT="$REPO" ;;
  *) echo "unknown agent: $AGENT" >&2; exit 1 ;;
esac

if [[ "$AGENT" == "12" ]]; then
  TARGET="$REPO"
else
  TARGET="$(cd "$REPO/$WT" 2>/dev/null && pwd)" || {
    echo "Worktree missing: $REPO/$WT" >&2
    echo "Create it first (see .missions/ agent GIT WORKTREE section)." >&2
    exit 1
  }
fi

# Arm in the worktree (where Claude Code should run)
(cd "$TARGET" && bash "$SCRIPT_DIR/arm-ralph.sh" "$AGENT")

# Also arm agent-prompts workspace if Claude is opened from that folder
if [[ "$TARGET" != "$SCRIPT_DIR" ]]; then
  (cd "$SCRIPT_DIR" && bash "$SCRIPT_DIR/arm-ralph.sh" "$AGENT") || true
fi

LAUNCHER="$(ls "$SCRIPT_DIR"/agent-${AGENT}-*.md 2>/dev/null | head -1)"
echo ""
echo "Armed Agent $AGENT in $TARGET/.claude/ralph-loop.local.md"
echo ""
echo "1. cd $TARGET"
echo "2. claude --dangerously-skip-permissions --effort max"
echo "3. Paste this one line (from $LAUNCHER):"
echo ""
head -1 "$LAUNCHER"
echo ""
