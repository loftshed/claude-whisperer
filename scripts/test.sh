#!/usr/bin/env sh
# Every test suite, all under tests/ (mirroring the repository layout, so
# nothing test-only ships inside a plugin): Vitest for JavaScript, unittest for
# the agent-executor Python. Keeps going after a failing suite.
set -u

cd "$(dirname "$0")/.." || exit 1

failed=0
./node_modules/.bin/vitest run || failed=1
python3 -B -m unittest discover -s tests/plugins/agent-executor/skills/agent-executor -p "test_*.py" || failed=1
exit $failed
