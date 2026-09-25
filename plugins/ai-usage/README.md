# ai-usage

How much subscription quota is left on every AI account on this machine, and where to spend it next.

It reads the rate-limit windows of each account (Claude Code, Codex/ChatGPT, Antigravity/Gemini), keeps them in a shared cache, and shows them three ways:

- **CLI / TUI**: `ai-usage` for a table, `ai-usage watch` for a live full-screen view.
- **macOS menu bar app**: % left grouped by limit window, one box per window length: `[5H  CW 100  CP 53  AG·G 76  AG·C 100]  [WK  CW 0  CP 82  CX 49  AG·G 3  AG·C 27]`. Sections come from each window's actual length, so a provider on another cycle gets its own box (`3H`, `1D`). Codex has no 5-hour limit, so it appears only under WK. Antigravity has two pools, Gemini (G) and Claude & GPT-OSS (C). Numbers are coloured by how much is left; details and a ranking are in the dropdown.
- **MCP server**: `get_usage` and `recommend` tools, so agents can check quota before choosing where to run work.

```
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

Each provider's own CLI reports its limits, so ai-usage never reads, copies or stores a credential:

| Provider | Source | Cost |
| --- | --- | --- |
| Claude Code (any number of profiles) | `claude -p /usage` with `CLAUDE_CONFIG_DIR` per profile, run without MCP servers, hooks or session files | 0 tokens, ~2 s |
| Codex / ChatGPT | `codex app-server` JSON-RPC `account/rateLimits/read` | 0 tokens, ~1 s |
| Antigravity (Gemini, plus its Claude/GPT pool) | `agy -p /usage --output-format json` | 0 tokens, ~4 s |

Providers are queried in parallel. Results are cached in `~/.cache/ai-usage/snapshot.json` (default max age 180 s) behind a lock, so the menu bar, TUI and MCP server never query twice at once. If a refresh fails, the last good reading is kept and marked stale. A window whose reset time has passed is shown as full again without a refetch.

The default Claude profile must run with `CLAUDE_CONFIG_DIR` **unset**: Claude Code derives its keychain entry from whether the variable is set, so pointing it at `~/.claude` explicitly finds no login. Leave `configDir` out for the default profile.

## Ranking

Percentages are relative to each account's own allowance; 1% of one plan is not 1% of another. The ranking asks a question that is comparable across accounts: **how much of this allowance is lost at reset unless spent?**

- `pts` (surplus) = weekly % remaining minus the % that would remain if the window were used evenly until it resets. +44 means 44 points of this week's allowance are unused beyond an even pace.
- A pool is **blocked** when any of its windows is exhausted, and ranks last with its unblock time.
- The score is `min(surplus, % usable right now)`, so a full weekly allowance behind an exhausted 5-hour window does not rank first.
- Advice flags `use it or lose it` when ≥10% of a weekly window expires within 24 h.

Pools are the independently limited groups inside an account: Antigravity's Gemini pool and its Claude/GPT pool, or Claude's all-models limit and a per-model weekly cap such as Fable. A per-model cap is hidden from the ranking unless it binds harder than its parent.

## Install

Part of the private [claude-whisperer](../../README.md) marketplace. Requires Node 22+ and the CLIs of the providers you use. Nothing is installed from npm.

Everything installs from a checkout. The marketplace is added from the local path, so plugin installs follow the checkout without a push.

```sh
gh repo clone loftshed/claude-whisperer ~/Documents/personal/claude-whisperer
cd ~/Documents/personal/claude-whisperer/plugins/ai-usage
./install.sh    # launcher, config, menu bar app + login agent (macOS), skill links for agy and OpenCode

# Plugin (MCP server + skill) in each Claude Code profile: once plain, once with CLAUDE_CONFIG_DIR=~/.claude-personal
claude plugin marketplace add ~/Documents/personal/claude-whisperer
claude plugin install ai-usage@claude-whisperer --scope user

# Plugin (skill) in Codex
codex plugin marketplace add ~/Documents/personal/claude-whisperer
codex plugin add ai-usage@claude-whisperer

# MCP server in every other harness found
~/.local/bin/ai-usage mcp-install
```

The ai-usage skill tells agents when to check quota and how to act on it. Claude Code and Codex get it from the plugin, Gemini CLI reads `~/.agents/skills`, and agy and OpenCode get links to that directory. agy can import Claude plugins but leaves `${CLAUDE_PLUGIN_ROOT}` unexpanded, so it uses the link and `mcp-install` instead.

`mcp-install` registers the server in Codex (CLI and desktop app share a config), agy, Gemini CLI and OpenCode (an agent-executor engine), plus any Claude Code profile without the plugin enabled (profiles with it are skipped so tools do not appear twice). Harnesses with an `mcp add` command are driven through it; OpenCode gets a config-file edit that backs the file up to `<file>.bak-ai-usage` and re-parses the result. It is safe to re-run. Without the plugin, `./install.sh --mcp` does the same including Claude Code.

Gemini CLI disables every MCP server in folders it does not trust.

No config is needed: without `~/.config/ai-usage/config.json`, ai-usage monitors what it detects (`~/.claude`, every `~/.claude-<name>` profile directory, `codex`, `agy`). `ai-usage init` writes that detection to the config file so you can edit labels, short names and routes; `--dry-run` shows it first.

`./uninstall.sh` removes the launcher, app, login agent, MCP registrations and cache (`--purge` also removes the config).

`install.sh` installs a copy of the CLI to `~/.local/share/ai-usage` and points the launcher there, so the menu bar app and MCP servers never read the checkout. macOS privacy protection makes background access to `~/Documents` wait on a permission prompt, which returns with every rebuild of the ad-hoc-signed app. Re-run `./install.sh` after pulling changes, moving the checkout, or changing Node versions.

## Commands

```
ai-usage [show]            table of every window plus where to spend next
ai-usage watch             live view (r = refresh now, q = quit)
ai-usage json              machine-readable snapshot, schema "ai-usage.snapshot.v1"
ai-usage line              one line for tmux or a status line: "5h CW 100 · CP 53 · … | wk CW 0 · CP 82 · CX 49 · …"
ai-usage recommend         ranked pools [--family claude|gpt|gemini] [--json]
ai-usage mcp               MCP server on stdio
ai-usage init              detect accounts and write the config
ai-usage mcp-install       register the MCP server in Claude, Codex, agy, Gemini CLI, OpenCode (mcp-uninstall removes)
```

`-r/--refresh` bypasses the cache; `--max-age <s>` sets how old cached data may be.

## Config

`~/.config/ai-usage/config.json` (override with `AI_USAGE_CONFIG`). See [config.example.json](config.example.json).

| Field | Meaning |
| --- | --- |
| `accounts[].provider` | `claude`, `codex` or `antigravity` |
| `accounts[].configDir` | Claude only: the profile's `CLAUDE_CONFIG_DIR`. Omit for the default profile. |
| `accounts[].short` | Menu bar and `line` abbreviation |
| `accounts[].billing` | `work` or `personal`. agent-executor treats `personal` accounts as `personal_subscription` and only routes to them when the user authorizes personal quota |
| `accounts[].route` / `routes` | How to use this account (shown in rankings and to agents); `routes` is per pool id |
| `accounts[].command` | Explicit path to the provider CLI |
| `env` | Extra environment for provider CLIs, e.g. `NODE_EXTRA_CA_CERTS` behind a TLS-inspecting proxy |
| `maxAgeSeconds` | Default cache age before re-querying |

Menu bar refresh interval: `defaults write local.ai-usage.bar refreshSeconds 300` (minimum 60).

## MCP tools

- `get_usage {refresh?}`: every window per account plus the ranking, as text followed by the JSON snapshot.
- `recommend {family?: any|claude|gpt|gemini, refresh?}`: ranked pools with route hints.

The server's instructions remind agents that switching accounts can switch billing context (work vs personal) and that they must follow the user's rules about which accounts they may use. The ranking is advice, not authorization.

## Development

```sh
node --test 'test/*.test.mjs'
macos/build.sh /tmp/AI\ Usage.app && "/tmp/AI Usage.app/Contents/MacOS/AIUsageBar" --dump-menu
```

Parsers are tested against captured real output in `test/fixtures`. Claude's `/usage` is human text, so its parser is the most likely thing to break on a Claude Code update; the fixtures make that visible.
