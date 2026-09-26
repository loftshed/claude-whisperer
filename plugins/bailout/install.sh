#!/bin/sh
# Install bailout from this checkout.
#
#   ./install.sh            link ~/.claude/skills/bailout to this checkout, then install the hooks
#   ./install.sh --dry-run  show what would change
#
# The hooks in ~/.claude/settings.json call ~/.claude/skills/bailout/scripts/hooks.mjs, so that path
# stays stable and points here. A real directory there (an old hand-copied install) is moved to
# ~/.local/share/bailout/backups, never deleted. ~/.claude-personal gets a link to the same place.
set -eu

repo="$(cd "$(dirname "$0")" && pwd -P)"
skill="$repo/skills/bailout"
place="$HOME/.claude/skills/bailout"
backups="${XDG_DATA_HOME:-$HOME/.local/share}/bailout/backups"
dry_run=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

command -v node >/dev/null || { echo "node is required" >&2; exit 1; }
[ -f "$skill/SKILL.md" ] || { echo "no skill at $skill" >&2; exit 1; }

run() {
  if [ "$dry_run" = 1 ]; then echo "  would: $*"; else "$@"; fi
}

# Point $2 at $1. A link is replaced; a real directory is moved aside first.
link() {
  target="$1" at="$2"
  if [ -L "$at" ]; then
    [ "$(readlink "$at")" = "$target" ] && { echo "• $at already linked"; return; }
  elif [ -e "$at" ]; then
    dest="$backups/$(date +%Y%m%d-%H%M%S)$(dirname "$at" | tr / _)"
    run mkdir -p "$dest"
    run mv "$at" "$dest/"
    echo "• moved the old copy at $at to $dest/"
  fi
  run mkdir -p "$(dirname "$at")"
  run ln -sfn "$target" "$at"
  echo "✓ $at -> $target"
}

link "$skill" "$place"
[ -d "$HOME/.claude-personal" ] && link "$place" "$HOME/.claude-personal/skills/bailout"

# The hooks call the stable ~/.claude/skills/bailout path, so an existing install keeps working through
# the link. Only install (which edits ~/.claude/settings.json) when status reports a missing or broken one.
if node "$skill/scripts/bailout.mjs" status 2>/dev/null | grep -q '^install: *ok'; then
  echo "• hooks already installed"
elif [ "$dry_run" = 1 ]; then
  node "$skill/scripts/install.mjs" --dry-run
else
  node "$place/scripts/install.mjs"
fi
