#!/bin/sh
# Removes what install.sh added. Keeps ~/.config/ai-usage (your config) unless --purge is given.
set -eu

purge=0
[ "${1:-}" = "--purge" ] && purge=1
launcher="$HOME/.local/bin/ai-usage"

if [ -x "$launcher" ]; then
  "$launcher" mcp-uninstall || true
fi

if [ "$(uname)" = Darwin ]; then
  launchctl bootout "gui/$(id -u)/local.ai-usage.bar" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/local.ai-usage.bar.plist"
  app="$HOME/Applications/AI Usage.app"
  [ -d "$app" ] && node -e 'require("fs").rmSync(process.argv[1], { recursive: true, force: true })' "$app"
  echo "✓ removed menu bar app and login agent"
fi

for link in "$HOME/.gemini/config/skills/ai-usage" "$HOME/.config/opencode/skills/ai-usage" "$HOME/.agents/skills/ai-usage"; do
  [ -L "$link" ] && rm -f "$link"
done
echo "✓ removed skill links"

rm -f "$launcher"
node -e 'require("fs").rmSync(process.argv[1], { recursive: true, force: true })' "$HOME/.cache/ai-usage"
echo "✓ removed launcher and cache"

if [ "$purge" = 1 ]; then
  node -e 'require("fs").rmSync(process.argv[1], { recursive: true, force: true })' "$HOME/.config/ai-usage"
  echo "✓ removed config"
fi
