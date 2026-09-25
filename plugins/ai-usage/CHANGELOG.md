# Changelog

All notable changes to this project. Versions follow [semver](https://semver.org); the `ai-usage json`
schema (`ai-usage.snapshot.v1`) changes its suffix on any breaking change to its shape.

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
