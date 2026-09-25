# Changelog

All notable changes to this project. Versions follow [semver](https://semver.org); the `ai-usage json`
schema (`ai-usage.snapshot.v1`) changes its suffix on any breaking change to its shape.

## 0.3.0 (2026-09-25)

- Menu bar pills: one per account and pool, 5-hour and weekly % left side by side, tinted by level; Codex
  shows weekly only, Antigravity shows Gemini (G) and Claude & GPT-OSS (C) pools. `ai-usage line` and the
  JSON (`accounts[].pills`) use the same layout. `AIUsageBar --render-title <png> [--light]` draws them.
- `install.sh` installs the CLI to `~/.local/share/ai-usage`. The menu bar app ran it from the checkout
  under `~/Documents`, where macOS privacy protection blocked it on a permission prompt and the menu bar
  stayed on "AI …".
- The menu bar app stops a refresh after 3 minutes and shows why, instead of waiting forever.
- Claude probe timeout 45 s → 90 s: the first launch in a fresh app context is slow.

## 0.2.1 (2026-09-25)

Fixes:

- A refresh lock left by a killed or quitting process no longer stalls every other caller for up to 90 s:
  the lock records its owner, a dead owner's lock is taken over, and `watch` releases it on exit.
- `codex app-server` exiting between requests no longer crashes ai-usage (and the MCP server) with EPIPE.
- Unknown options and non-numeric `--max-age`/`--interval` exit 2 with a message; a non-numeric
  `--max-age` used to return stale data forever.
- `install.sh` replaces dangling skill links after the checkout moves instead of aborting.
- `recommend --family gpt` means Codex; Antigravity's GPT-OSS models are their own `gpt-oss` family.
- `recommend` with an unknown family says so; `mcp-uninstall` reports absent registrations as not registered.

## 0.2.0 (2026-09-25)

- `accounts[].billing` (`work` | `personal`) in the config and `ai-usage json`, so agent-executor keeps
  personal accounts behind its personal-quota authorization.
- MCP registration limited to the harnesses in use: Claude Code, Codex, agy, Gemini CLI and OpenCode.
- The skill points agents at agent-executor's built-in quota check.

## 0.1.0 (2026-09-25)

First release.

- Providers: Claude Code (multiple `CLAUDE_CONFIG_DIR` profiles) via `claude -p /usage`, Codex via
  `codex app-server` rate limits, Antigravity via `agy -p /usage --output-format json`. No credential access.
- Shared, locked cache with last-good fallback; windows past their reset time count as full.
- Ranking by surplus against even pace, blocked pools last, per-model sub-limits folded into their parent.
- CLI: `show`, `watch`, `json`, `line`, `recommend`, `init`, `config`, `mcp-install`, `mcp-uninstall`.
- MCP server with `get_usage` and `recommend`.
- macOS menu bar app (AppKit, no dependencies) with a login LaunchAgent; `--dump-menu` for text checks.
- `install.sh` / `uninstall.sh`.
- `mcp-install` covers Claude Code profiles (skipped where the plugin is enabled), Codex, agy,
  Gemini CLI and OpenCode, with a backed-up, re-parsed config edit where no CLI exists.
- `install.sh` links the skill into `~/.agents/skills` and on to agy and OpenCode.
- Packaged as a claude-whisperer plugin: bundled MCP server (`.mcp.json`) and an `ai-usage` skill with
  quota-aware routing rules. Without a config file, accounts are auto-detected.
