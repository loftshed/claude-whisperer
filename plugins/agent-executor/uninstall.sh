#!/bin/sh
# Remove the agent-executor links install.sh created. Only links are removed: real directories,
# the route config (~/.config/agent-executor), job state (~/.cache/agent-executor) and backups stay.
set -eu

hub="$HOME/.agents/skills/agent-executor"
for place in \
  "$HOME/.claude/skills/agent-executor" \
  "$HOME/.claude-personal/skills/agent-executor" \
  "$HOME/.codex/skills/agent-executor" \
  "$HOME/.config/opencode/skills/agent-executor" \
  "$HOME/.gemini/config/skills/agent-executor" \
  "$hub"; do
  if [ -L "$place" ]; then
    rm "$place"
    echo "✓ removed $place"
  elif [ -e "$place" ]; then
    echo "• $place is not a link; left alone" >&2
  fi
done
