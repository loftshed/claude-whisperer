# Changelog

All notable changes to this project. Versions follow [semver](https://semver.org); the `ai-usage json`
schema (`ai-usage.snapshot.v1`) changes its suffix on any breaking change to its shape.

## Unreleased

- ChatGPT's exhausted weekly allowance switches to remaining credits in full and compact menu bar pills,
  collapsed account headings and the CLI status line, keeping the time until the included week is back.
  Percentages resume at the weekly reset and the balance leaves those readings; the expanded dropdown,
  terminal view and MCP usage report list it whenever it is known. Zero, unlimited and missing balances
  are handled.
- Credit balances use a small coin in the menu bar and dropdown; accessibility labels read "credits".
- A Codex pool whose week is used up but that has credits is ranked as `status: "credits"` instead of
  `blocked`: usable for any task, metered against the balance, after every pool with free quota. The
  terminal ranking, the dropdown, `recommend` and the MCP tools say "on credits" with the balance, and
  agent-executor's launch gate no longer refuses it.

## 0.9.0 (2026-09-29)

- Double-click the menu bar item to shrink it to one small pill with only each account's weekly % left
  (`ᶜʷ44 ᶜᴾ90 ᶜˣ35 ᴬᴳ53·100`, about 40% of the full width), so a crowded menu bar keeps room for other icons;
  double-click again for the full pills. `--render-title … --compact` draws the shrunken bar. Remembered (`compactBar` in the `local.ai-usage.bar`
  defaults). A single click opens the dropdown after the double-click interval; right-click opens it at once.
- Menu bar dropdown sections expand and collapse: each account heading and "Where to spend next" has a ▾/▸
  toggle that opens or closes the section in place without closing the menu. A collapsed account shows a one-line
  summary (`5h 96% · wk 44% · Fable wk 89%`, ⚠ on a failed read); a collapsed ranking shows its top pick. The
  state is remembered (`collapsedSections` in the `local.ai-usage.bar` defaults); everything starts expanded.
- `AIUsageBar --dump-menu` prints only visible rows and takes `--collapse all|<key>,<key>` (keys `account:<id>`,
  `ranking`) to check the collapsed layout without changing the saved state.

## 0.8.0 (2026-09-25)

- Menu bar redesigned around providers: one pill each, showing every pool's 5-hour % left and weekly % left,
  with the time until the weekly rollover as its unit (`3d`, `5h`) instead of a WK header. ☠ plus the time until
  it is back replaces a week that is used up; an expiring pool's time turns orange with ⏳. `ai-usage line`
  uses the same layout; `accounts[].pills` gauges carry `windowMins`, `exhausted` and `expiring`.
- Muted menu bar colours resolve when drawn, so they follow the menu bar between light and dark.

## 0.7.1 (2026-09-25)

- Repeated macOS permission prompts for "AI Usage": `install.sh` rebuilt and re-signed the ad-hoc-signed app on
  every run, so macOS forgot each answer. It now rebuilds only when `macos/main.swift` or `build.sh` change
  (`AIUsageBuildHash` in the bundle), keeping the app's identity.
- Antigravity is re-queried at most every 10 minutes unless a refresh is forced: `agy -p /usage` starts every
  MCP server in agy's config (here an LSP via `npm exec` and a mock server under `~/Documents`, the source of
  the Documents prompt). `accounts[].minRefreshSeconds` overrides it per account.

## 0.7.0 (2026-09-25)

Use it or lose it:

- Ranking tiers: expiring pools (last 20% of a day-or-longer window with at least 5% left) first, by the
  %/h needed to use them up; then pools with room; then pools under 10% usable now ("small tasks only");
  then blocked. Lanes carry `expiring`, `expiresAt`, `burnPctPerHour`, `lowRoom`; the menu bar marks
  expiring pools ⏳ and the MCP tools open with an "Expiring soon, spend first" line.

Fixes from a deep bug hunt:

- A slow refresh's lock (a live owner past 120 s) could be taken over, and the original owner's unlock then
  deleted the new owner's lock. Live owners keep their lock (up to 10 min); unlock removes only its own.
- `codex app-server` requests that reuse our JSON-RPC ids were taken for its answers.
- A Codex or Antigravity limit with no windows became an empty pool that hid the real one (headline
  "Infinity", no Codex in the menu bar).
- Claude `/usage` lines with a separator other than "·", or "(resets …)", lost their reset time or window.
- Reset text "in 2h 15m" was read as 2 AM; "tomorrow" and weekday forms are handled; rolling to the next
  day recomputes in the zone instead of adding 24 h across a daylight-saving change.
- MCP: `arguments: null` failed the call; JSON-RPC batches got no reply; invalid messages got no error.
  `get_usage` output is about half the size (compact JSON without display-only fields) and includes billing.
- Menu bar: a failed refresh kept showing old numbers without notice (now ⚠); no accounts or an older
  ai-usage showed an invisible bar (now "AI –").
- Accounts removed from the config are dropped from the cache.

## 0.6.0 (2026-09-25)

- Menu bar: pools whose weekly (or other day-or-longer) limit is used up leave the 5H/WK boxes and are
  listed once in a ☠ box; an empty 5-hour window alone stays in 5H as a red 0. `ai-usage line` ends with
  `| ☠ …`, and `ai-usage json` adds `exhausted` (with the window and when it resets). Replaces 0.5.0's
  inline skulls in the menu bar; the terminal view and dropdown keep marking dead windows.

## 0.5.0 (2026-09-25)

- ☠ instead of a number for a used-up limit, and for a shorter limit made unusable by an exhausted longer
  one in the same pool (e.g. 5-hour allowance once the week is gone). Per-model caps (Claude's Fable weekly)
  are unusable once the account-wide limit of the same length is gone. JSON sections and windows carry
  `exhausted` and `blockedBy`; the terminal view and dropdown say "unusable until the wk limit resets".
- agy occasionally answers `/usage` with status ERROR and no detail; it is retried once, and a lasting
  failure now includes agy's own error text.

## 0.4.0 (2026-09-25)

- Menu bar groups limits by window length instead of per-account pills: a `5H` box and a `WK` box side
  by side, each listing every account's % left. Sections are derived from each window's real duration, so
  other cycles get their own box (`3H`, `1D`, …). `ai-usage line` uses the same grouping, and
  `ai-usage json` adds top-level `sections` and `unavailable`.

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
