#!/bin/sh
# Put claude-migrate on your PATH: links ~/.local/bin/claude-migrate to this checkout.
#
#   ./install.sh            install
#   ./install.sh --dry-run  show what would change
#
# A copy an import left at ~/.local/bin/claude-migrate is replaced by the link.
set -eu

repo="$(cd "$(dirname "$0")" && pwd -P)"
target="$repo/bin/claude-migrate"
at="$HOME/.local/bin/claude-migrate"
case "${1:-}" in
  --dry-run) echo "would link $at -> $target"; exit 0 ;;
  -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  '') ;;
  *) echo "unknown option: $1" >&2; exit 2 ;;
esac

mkdir -p "$HOME/.local/bin"
ln -sfn "$target" "$at"
echo "• $at -> $target"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "  ~/.local/bin is not on your PATH; add it to ~/.zshrc" ;;
esac
