# monitor

How much subscription quota is left on every AI account on this machine, and where to spend it next.

It reads the rate-limit windows of each account (Claude Code, Codex/ChatGPT, Antigravity/Gemini), keeps them in a shared cache, and shows them three ways:

- **CLI / TUI**: `monitor` for a table, `monitor watch` for a live full-screen view.
- **macOS menu bar app**: one pill per provider: `[CW ☠3h]  [CP 98·75 3d]  [CX 45 5d]  [AG G 76·3 2h │ C 100·27 1d⏳]`. Each pool shows its 5-hour % left (if it has a 5-hour limit), then its weekly % left followed by the time until the weekly rollover (`3d` rounded to the nearest day, or `2h` once under a day). A pool whose week is used up shows ☠ and the time until it is back; a pool near its rollover with capacity left shows its time in orange with ⏳. Antigravity has two pools, Gemini (G) and Claude & GPT-OSS (C). Numbers are coloured by how much is left; the dropdown has details, reset times and a ranking, and each account and the ranking collapse to one line with their ▾/▸ toggle (remembered across launches). **Double-click** the item (or choose **Menu Bar Style** in the dropdown) to cycle between **Full Pills**, **Compact Weekly Numbers** (`CW 44 CP 90 CX 35 AG 53·100`), and **Icon Only** (a small status icon with no persistent numbers — hover over it to see your remaining quota and top recommendation). A single click opens the dropdown a moment later; right-click opens it at once.
- **MCP server**: `get_usage` and `recommend` tools, so agents can check quota before choosing where to run work.

When ChatGPT's weekly allowance is exhausted, its full pill shows the remaining credit balance with a coin, still followed by the time until the included week is back, such as `CX 62,261.91 🪙 2d`; the compact pill shows the balance alone. The dropdown uses the coin too; the terminal view and MCP usage report label credits as `cr`. A zero balance means no credits remain; `∞` means unlimited credits. If the balance is unavailable, the pill keeps the exhausted-week symbol. Percentages resume at the weekly reset and the balance leaves the pill; the expanded dropdown, terminal view and MCP usage report list it whenever it is known. In the ranking, a Codex pool with credits is **on credits** rather than blocked: it can still take work, metered against the balance, and ranks after every pool with free quota.

```text
 Claude · personal  subscription
   5-hour                 ██████████████ 100% left   reset since last check
   weekly                 ████████████▌·  89% left   resets Mon 4:59 PM (in 3d 4h)

 Where to spend next  (pts = weekly % left minus what even use would leave)
  1  Claude · personal · all models      +44 pts   under-used, spend freely       plaude
  2  Antigravity · Claude & GPT           +9 pts   on pace                        agy --model …
  4  Codex · ChatGPT                     −19 pts   ahead of pace, conserve        codex
  ✗  Claude · work · all models                    out until 9:00 PM today        claude
```

## How it reads quota

Each provider's own CLI reports its limits, so monitor never reads, copies or stores a credential:

| Provider                                       | Source                                                                                                   | Cost           |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------- | -------------- |
| Claude Code (any number of profiles)           | `claude -p /usage` with `CLAUDE_CONFIG_DIR` per profile, run without MCP servers, hooks or session files | 0 tokens, ~2 s |
| Codex / ChatGPT                                | `codex app-server` JSON-RPC `account/rateLimits/read`                                                    | 0 tokens, ~1 s |
| Antigravity (Gemini, plus its Claude/GPT pool) | `agy -p /usage --output-format json`                                                                     | 0 tokens, ~4 s |

Providers are queried in parallel. Results are cached in `~/.cache/monitor/snapshot.json` (default max age 180 s) behind a lock, so the menu bar, TUI and MCP server never query twice at once. If a refresh fails, the last good reading is kept and marked stale. A window whose reset time has passed is shown as full again without a refetch.

The default Claude profile must run with `CLAUDE_CONFIG_DIR` **unset**: Claude Code derives its keychain entry from whether the variable is set, so pointing it at `~/.claude` explicitly finds no login. Leave `configDir` out for the default profile.

## Observe reset patterns

For passive history collection on macOS, run:

```sh
node plugins/monitor/macos/install-observer.mjs
```

This installs a separate login agent, `local.monitor.observer`, which checks the existing Monitor cache once a minute. It does not restart the menu bar app, query providers, spend tokens or redeem resets. Keep Monitor running so its cache receives fresh readings. When the computer sleeps or Monitor stops refreshing, the recorder cannot fill that gap.

Daily JSONL files in `~/.local/share/monitor-observations/` keep at most **500 records total**, expiring records after **14 days**. A fresh reading is saved at most once every **12 hours per account**, plus notable changes:

- A daily-or-longer allowance rises by at least 5 percentage points or drops by at least 25 points between readings.
- Its reported reset time moves by more than 30 minutes, appears or disappears.
- The plan, quota windows or available banked-reset count changes.
- Refresh status changes from success to failure or back. Repeated failures are not logged again.

Routine usage changes and ordinary short-window rollovers do not add records. Each event includes the immediately preceding reading for comparison. The latest reading is overwritten in a small state file rather than appended to history. Checkpoints require a fresh provider reading; stale cache data is not logged again. No reset cause or permanent weekly schedule is inferred. No prompts, credentials, provider error text or conversation contents are copied.

`status.json` shows the last recorder check and the age of each account's reading. Files are private to the current user. `observationId` supports deduplication if a crash repeats a row. The installed script lives outside the checkout, so switching branches does not change the recorder.

On other platforms, schedule `node plugins/monitor/bin/observe.mjs` once a minute with one scheduler. It also supports `--cache-dir` and `--output-dir` for isolated analysis or testing. To stop the macOS recorder while retaining its data:

```sh
launchctl bootout "gui/$(id -u)/local.monitor.observer"
rm ~/Library/LaunchAgents/local.monitor.observer.plist
```

## Ranking

Percentages are relative to each account's own allowance; 1% of one plan is not 1% of another. The ranking asks a question that is comparable across accounts: **how much of this allowance is lost at reset unless spent?**

- `pts` (surplus) = weekly % remaining minus the % that would remain if the window were used evenly until it resets. +44 means 44 points of this week's allowance are unused beyond an even pace.
- Pools are ranked in tiers:
  1. **Expiring**: in the last 20% of a day-or-longer window (about 34 h of a week) with at least 5% left. That remainder is lost at the rollover, so these go first, most urgent first (highest %/h needed to use it all), with advice like "use it or lose it: 27% of the weekly limit resets Sat 8:25 PM (in 28h); ~1%/h uses it all". The menu bar marks them ⏳.
  2. **Pools with room**, by `min(surplus, % usable right now)`, so a full week behind an exhausted 5-hour window does not rank first.
  3. **On credits** (`status: "credits"`): the included allowance is used up but the account has a credit balance (Codex), so every request is metered against credits that persist. Any task size fits; free quota goes first because it is lost at reset.
  4. **Nearly empty**: under 10% usable right now ("small tasks only"), whatever their surplus. A cheap pool that stalls mid-task costs more than it saves.
  5. **Blocked** (a window exhausted, no credits), soonest back first; then accounts with no data.
- Every provider reports exact reset times (Codex and Antigravity as timestamps, Claude as local times), and a window past its reset counts as full again without waiting for a refetch.

Pools are the independently limited groups inside an account: Antigravity's Gemini pool and its Claude/GPT pool, or Claude's all-models limit and a per-model weekly cap such as Fable. A per-model cap is hidden from the ranking unless it binds harder than its parent.

## Install

Part of the private [orkest](../../README.md) marketplace. Requires Node 22+ and the CLIs of the providers you use. Nothing is installed from npm.

Everything installs from a checkout. The marketplace is added from the local path, so plugin installs follow the checkout without a push.

```sh
gh repo clone loftshed/orkest ~/Documents/personal/orkest
cd ~/Documents/personal/orkest/plugins/monitor
./install.sh    # launcher, config, menu bar app + login agent (macOS), skill links for agy and OpenCode

# Plugin (MCP server + skill) in each Claude Code profile: once plain, once with CLAUDE_CONFIG_DIR=~/.claude-personal
claude plugin marketplace add ~/Documents/personal/orkest
claude plugin install monitor@orkest --scope user

# Plugin (skill) in Codex
codex plugin marketplace add ~/Documents/personal/orkest
codex plugin add monitor@orkest

# MCP server in every other harness found
~/.local/bin/monitor mcp-install
```

The monitor skill tells agents when to check quota and how to act on it. Claude Code and Codex get it from the plugin, Gemini CLI reads `~/.agents/skills`, and agy and OpenCode get links to that directory. agy can import Claude plugins but leaves `${CLAUDE_PLUGIN_ROOT}` unexpanded, so it uses the link and `mcp-install` instead.

`mcp-install` registers the server in Codex (CLI and desktop app share a config), agy, Gemini CLI and OpenCode (an orchestrate engine), plus any Claude Code profile without the plugin enabled (profiles with it are skipped so tools do not appear twice). Harnesses with an `mcp add` command are driven through it; OpenCode gets a config-file edit that backs the file up to `<file>.bak-monitor` and re-parses the result. It is safe to re-run. Without the plugin, `./install.sh --mcp` does the same including Claude Code.

Gemini CLI disables every MCP server in folders it does not trust.

No config is needed: without `~/.config/monitor/config.json`, monitor monitors what it detects (`~/.claude`, every `~/.claude-<name>` profile directory, `codex`, `agy`). `monitor init` writes that detection to the config file so you can edit labels, short names and routes; `--dry-run` shows it first.

`./uninstall.sh` removes the launcher, app, login agent, MCP registrations and cache (`--purge` also removes the config).

`install.sh` installs a copy of the CLI to `~/.local/share/monitor` and points the launcher there, so the menu bar app and MCP servers never read the checkout. macOS privacy protection makes background access to `~/Documents` wait on a permission prompt, which returns with every rebuild of the ad-hoc-signed app. Re-run `./install.sh` after pulling changes, moving the checkout, or changing Node versions.

## macOS permission prompts

The menu bar app runs the provider CLIs, and macOS charges whatever they touch to "Monitor". `agy -p /usage` starts every MCP server in agy's config, so a server that lives under `~/Documents` (or needs another protected resource) makes macOS ask "Monitor" for that access. Allow or Don't Allow both work: denying only stops that MCP server inside agy's usage check. The app is ad-hoc signed, so macOS identifies it by its exact code; `install.sh` rebuilds it only when its source changes, which keeps your answer. If you change `macos/main.swift`, expect to answer once more.

## Commands

### Linux desktop widget

On KDE Plasma 6, install the panel/desktop widget after installing the CLI:

```sh
sh plugins/monitor/linux/install.sh
```

Open **Add Widgets**, search for **Monitor**, and place it on your panel or desktop.
It works under Wayland and X11, refreshes every two minutes using the shared cache,
can switch between panel numbers and an icon-only button (with hover quota details),
and opens a popup with quota bars, reset times, credit balances and a refresh button.
Previous widget installations are moved to backups. The existing menu bar app is macOS-only.

Claude's session cost summary is not proof of API billing. If `/usage` returns costs
instead of quota windows, the widget reports usage as unavailable rather than guessing.

```text
monitor [show]             table of every window plus where to spend next
monitor watch              live view (r = refresh now, q = quit)
monitor json               machine-readable snapshot, schema "monitor.snapshot.v1"
monitor line               one line for tmux or a status line: "CW ☠3h · CP 98·75 3d · CX 45 5d · AG G76·3 2h C100·27 1d⏳"
monitor recommend          ranked pools [--family claude|gpt|gemini] [--json]
monitor mcp                MCP server on stdio
monitor init               detect accounts and write the config
monitor mcp-install        register the MCP server in Claude, Codex, agy, Gemini CLI, OpenCode (mcp-uninstall removes)
```

`-r/--refresh` bypasses the cache; `--max-age <s>` sets how old cached data may be.

## Config

`~/.config/monitor/config.json` (override with `MONITOR_CONFIG`). See [config.example.json](config.example.json).

| Field                          | Meaning                                                                                                                                                             |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `accounts[].provider`          | `claude`, `codex` or `antigravity`                                                                                                                                  |
| `accounts[].configDir`         | Claude only: the profile's `CLAUDE_CONFIG_DIR`. Omit for the default profile.                                                                                       |
| `accounts[].short`             | Menu bar and `line` abbreviation                                                                                                                                    |
| `accounts[].billing`           | `work` or `personal`. orchestrate treats `personal` accounts as `personal_subscription` and only routes to them when the user authorizes personal quota             |
| `accounts[].route` / `routes`  | How to use this account (shown in rankings and to agents); `routes` is per pool id                                                                                  |
| `accounts[].command`           | Explicit path to the provider CLI                                                                                                                                   |
| `env`                          | Extra environment for provider CLIs, e.g. `NODE_EXTRA_CA_CERTS` behind a TLS-inspecting proxy                                                                       |
| `maxAgeSeconds`                | Default cache age before re-querying                                                                                                                                |
| `accounts[].minRefreshSeconds` | Never re-query this account more often than this, except on an explicit refresh. Default 600 for Antigravity: `agy /usage` starts every MCP server in agy's config. |

Menu bar refresh interval: `defaults write local.monitor.bar refreshSeconds 300` (minimum 60).

## MCP tools

- `get_usage {refresh?}`: every window per account plus the ranking, as text followed by the JSON snapshot.
- `recommend {family?: any|claude|gpt|gemini, refresh?}`: ranked pools with route hints.

The server's instructions remind agents that switching accounts can switch billing context (work vs personal) and that they must follow the user's rules about which accounts they may use. The ranking is advice, not authorization.

## Development

```sh
# from the repository root
./node_modules/.bin/vitest run tests/plugins/monitor
macos/build.sh /tmp/Monitor.app && "/tmp/Monitor.app/Contents/MacOS/MonitorBar" --dump-menu   # add --collapse all to see collapsed sections
```

Parsers are tested against captured real output in `tests/plugins/monitor/fixtures` at the repository root. Claude's `/usage` is human text, so its parser is the most likely thing to break on a Claude Code update; the fixtures make that visible.
