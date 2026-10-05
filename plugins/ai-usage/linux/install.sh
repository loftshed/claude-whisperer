#!/bin/sh
# Install the Plasma 6 widget user-locally, preserving any previous copy.
set -eu
repo="$(cd "$(dirname "$0")" && pwd -P)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
target="$data/plasma/plasmoids/com.loftshed.aiusage"
if [ -e "$target" ] || [ -L "$target" ]; then
  backup="$data/ai-usage/backups/plasmoid-$(date +%Y%m%d-%H%M%S)-$$"
  mkdir -p "$(dirname "$backup")"
  mv "$target" "$backup"
  echo "Previous widget saved: $backup"
fi
mkdir -p "$(dirname "$target")"
cp -R "$repo/plasmoid" "$target"
echo "Installed: $target"
echo "Add Widgets → AI Usage (Plasma 6, Wayland or X11)."
