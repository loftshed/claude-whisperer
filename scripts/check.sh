#!/usr/bin/env sh
# The lint gate, shared by the pre-commit hook and CI. Every check runs even
# after one fails, and the exit status is non-zero if any of them failed.
# Tools come from ./node_modules/.bin, so yarn does not need to be on PATH.
set -u
cd "$(dirname "$0")/.." || exit 1

# The one place the ruff version is pinned (Renovate keeps it current).
RUFF_VERSION=0.16.9

tools=./node_modules/.bin
[ -x "$tools/eslint" ] || { echo "check: run \`yarn install\` first." >&2; exit 1; }

status=0
step() { "$@" || status=1; }

step node scripts/validate-marketplace.js
if command -v claude > /dev/null 2>&1; then
  # Quiet on success, full report on failure.
  claude plugin validate . > /dev/null 2>&1 || step claude plugin validate .
fi
step "$tools/eslint" .
step "$tools/prettier" --check --log-level warn .
"$tools/markdownlint-cli2" > /dev/null 2>&1 || step "$tools/markdownlint-cli2"
step "$tools/cspell" --no-progress --no-summary "**/*"

if command -v uvx > /dev/null 2>&1; then
  step uvx "ruff@$RUFF_VERSION" check --quiet .
  step uvx "ruff@$RUFF_VERSION" format --check --quiet .
else
  echo "check: uv is not installed; skipping ruff (CI still runs it)." >&2
fi

# Shell scripts: *.sh, the husky hooks and executables with a sh/bash shebang.
is_shell_script() {
  case "$1" in
    *.sh | .husky/pre-*) return 0 ;;
    *.*) return 1 ;;
  esac
  head -n 1 "$1" | grep -Eq '^#!.*[/ ](ba)?sh$'
}
if command -v shellcheck > /dev/null 2>&1; then
  scripts=$(git ls-files --cached --others --exclude-standard | while IFS= read -r file; do
    if [ -f "$file" ] && is_shell_script "$file"; then echo "$file"; fi
  done)
  # shellcheck disable=SC2086 # one path per word
  step shellcheck $scripts
else
  echo "check: shellcheck is not installed; skipping shell lint (CI still runs it)." >&2
fi

exit $status
