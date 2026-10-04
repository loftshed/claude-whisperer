# CLAUDE.md

Guidance for Claude Code in this repository.

## The repository

`loftshed/claude-whisperer` is a personal Claude Code plugin marketplace. Each plugin is a directory under `plugins/`, an entry in [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json), and a row in the README's plugin table.

Reference: [plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces), [plugins reference](https://code.claude.com/docs/en/plugins-reference).

## Commands

```sh
yarn install   # dev tools, plus the git hooks
yarn check     # the lint gate (scripts/check.sh); CI runs the same script
yarn test      # Vitest for JavaScript, unittest for the agent-executor Python (scripts/test.sh)
node scripts/release.js plan   # what the next release would publish
```

Both scripts call `./node_modules/.bin` directly, so they work without `yarn` on PATH. Single steps: `yarn validate`, `lint`, `lint:fix`, `format`, `format:check`, `markdownlint`, `spell`.

## Rules for changes

- **Never edit a plugin version.** Write conventional commit subjects (`feat(ai-usage): …`, `fix: …`, `refactor!: …`); CI releases on `main` and writes every version, changelog heading and tag. Notes under `## Unreleased` in a plugin's `CHANGELOG.md` replace the generated ones. Details: [docs/releases.md](docs/releases.md).
- **Tests go in `tests/`**, at the same relative path as the code they cover, never inside `plugins/`: a plugin directory is copied whole on install. JavaScript tests are `*-test.js` files run by Vitest. Files under `tests/**/fixtures/` are captured output; keep them byte-for-byte.
- **Plugin code is ESM `.mjs`** with Node built-ins only, because it runs from a copied cache with no `package.json` or `node_modules`. Repository tooling in `scripts/` is `.js`. When a script checks whether it is the entry point, compare real paths, since plugins are often started through symlinks.
- **Let the gate decide style.** ESLint, Prettier, markdownlint, cspell, ruff and shellcheck run on commit and in CI. Fix what a rule reports instead of disabling it; an unavoidable inline disable carries a `-- reason`. New words go in [`project-words.txt`](project-words.txt).
- **Plugin-relative paths** use `${CLAUDE_PLUGIN_ROOT}` or `${CLAUDE_SKILL_DIR}`, quoted wherever a shell expands them. The validator fails when one points at a missing file.
- **Keep documentation short.** Say what the reader needs to do or know, once. Put gotchas under a Troubleshooting heading.

## Hooks

- `.husky/pre-commit` runs the lint gate. `.husky/pre-push` hands off to the pre-push hook in the global `core.hooksPath`, which husky would otherwise bypass.
- [`.claude/hooks/lint-changed.mjs`](.claude/hooks/lint-changed.mjs) collects each session's Write/Edit targets and fixes and formats them together when Claude stops. It leaves unrelated dirty files and Git staging alone, and sends remaining problems back with bounded retries. Shell edits and interrupted turns still need the normal lint gate.

## Tooling notes

- Yarn refuses package versions published in the last 24 hours; a "quarantined" install succeeds once the release is a day old.
- The ruff version is pinned once, in `scripts/check.sh`.
