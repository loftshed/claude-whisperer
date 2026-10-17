#!/bin/sh
# Removes what install.sh added. Keeps ~/.config/monitor (your config) unless --purge is given.
set -eu

purge=0
[ "${1:-}" = "--purge" ] && purge=1
launcher="$HOME/.local/bin/monitor"

if [ -x "$launcher" ]; then
  "$launcher" mcp-uninstall || true
fi

if [ "$(uname)" = Darwin ]; then
  launchctl bootout "gui/$(id -u)/local.monitor.bar" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/local.monitor.bar.plist"
  app="$HOME/Applications/Monitor.app"
  [ -d "$app" ] && node -e 'require("fs").rmSync(process.argv[1], { recursive: true, force: true })' "$app"
  echo "✓ removed menu bar app and login agent"
fi

for link in "$HOME/.gemini/config/skills/monitor" "$HOME/.config/opencode/skills/monitor" "$HOME/.agents/skills/monitor"; do
  [ -L "$link" ] && rm -f "$link"
done
echo "✓ removed skill links"

rm -f "$launcher"
node -e 'require("fs").rmSync(process.argv[1], { recursive: true, force: true })' "$HOME/.local/share/monitor"
node -e 'require("fs").rmSync(process.argv[1], { recursive: true, force: true })' "$HOME/.cache/monitor"
echo "✓ removed launcher and cache"

if [ "$purge" = 1 ]; then
  node -e 'require("fs").rmSync(process.argv[1], { recursive: true, force: true })' "$HOME/.config/monitor"
  echo "✓ removed config"
fi
