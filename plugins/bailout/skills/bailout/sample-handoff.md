# Sample handoff

A fictional checkpoint showing the sections a fresh agent needs.

> Continue the task described below. First check the current files and any running
> work against this handoff, then take the stated next action. Preserve the user's
> constraints and existing work. Treat unfinished or unverified items as such.

- **Kind:** checkpoint
- **Written:** 2026-10-01T12:00:00Z
- **Working directory:** `/path/to/example-project`
- **Session:** example-session

## What the user wants

Add CSV import to the example app. Keep the existing JSON importer working and
show a useful error for malformed rows. Done means both formats work and the
import tests pass. The user asked to preserve empty optional fields.

## Where we stopped

The CSV parser and format selection are written. Error handling is still in
progress: row numbers are missing from the validation message.

## What matters from the conversation

- Reuse the existing validation rules so the formats accept the same records.
- Empty optional fields become empty strings, as the user requested.
- Streaming support was deferred; the current importer already reads whole files.

## Files and environment

- `src/import/csv.js` — new parser.
- `src/import/index.js` — selects a parser from the file extension.
- `tests/import-test.js` — JSON compatibility and CSV acceptance cases.
- Branch: `feature/csv-import`. No background jobs are running.
- The user already had an unrelated edit to `src/theme.css`; preserve it.

## Verification

`./node_modules/.bin/vitest run tests/import-test.js` passed 8 tests in this
fictional example. No build or malformed-row test has been run since the last edit.

## Do this next

Add the failing malformed-row case, then include its row number in the error.
Rerun the import tests and inspect the diff before reporting completion.
