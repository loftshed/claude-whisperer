# orkest

Personal plugin marketplace and local multi-agent toolkit for Claude Code, Codex, Antigravity (Gemini), and OpenCode.

| Plugin                                         | Version | What it does                                                                                                                                         |
| ---------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`monitor`](plugins/monitor/README.md)         | 0.13.0  | Remaining quota across Claude Code profiles, Codex and Antigravity, and where to spend it: MCP server, skill, CLI/TUI, macOS menu bar app.           |
| [`orchestrate`](plugins/orchestrate/README.md) | 0.31.1  | Cross-harness consultation, bounded delegation, peer mailbox, team succession, and swarm rooms across Claude Code, Codex, Antigravity, and OpenCode. |

`monitor` is the quota source: `orchestrate`'s launch gate, `quota` command, Stooges room header, and deputy succession all read `monitor`'s cache (`~/.cache/monitor/snapshot.json`) and `~/.config/monitor/config.json`, where an account's `billing` (`work` or `personal`) defines its context.

## Install

Keep `<checkout>` outside `~/Documents`, `~/Desktop`, and `~/Downloads`: macOS asks again for each tool version that reads those folders.

```sh
gh repo clone loftshed/orkest <checkout>

# 1. Quota monitor (CLI, macOS menu bar app, skill links, and MCP registrations)
<checkout>/plugins/monitor/install.sh
~/.local/bin/monitor mcp-install

# 2. Orchestrate (skill hub, route config, peer-mailbox daemon, and turn hooks)
<checkout>/plugins/orchestrate/install.sh
```

### Plugin marketplace (optional)

You can also register `<checkout>` (or `loftshed/orkest` on GitHub) as a plugin marketplace in Claude Code and Codex:

```sh
claude plugin marketplace add <checkout>
claude plugin install monitor@orkest --scope user
claude plugin install orchestrate@orkest --scope user

codex plugin marketplace add <checkout>
codex plugin add monitor@orkest
```

If Claude Code loads `orchestrate` from the plugin marketplace, run `<checkout>/plugins/orchestrate/install.sh --no-claude` so `install.sh` links the remaining harnesses and sets up the mailbox without creating a duplicate skill in `~/.claude/skills/orchestrate`.

## Development

```sh
yarn install                  # dev dependencies and git hooks
yarn check                    # lint gate: marketplace validator, ESLint, Prettier, markdownlint, cspell, ruff, shellcheck
yarn test                     # Vitest (JavaScript) and unittest (orchestrate Python)
node scripts/release.js plan  # preview the next automated plugin release
```

Tests live in `tests/`, mirroring the `plugins/` and `scripts/` directory layout. See [CLAUDE.md](CLAUDE.md) for repository conventions and [docs/README.md](docs/README.md) for [release automation](docs/releases.md) and design records.
