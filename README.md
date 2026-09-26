# Claude Whisperer

Private personal plugin marketplace and versioned skill backup.

| Plugin | Version | What it does |
| --- | --- | --- |
| `agent-executor` | 0.10.0 | Bounded agent execution, consultation, evidence, and audit across Codex, Antigravity, Claude Code and OpenCode. |
| [`ai-usage`](plugins/ai-usage/README.md) | 0.8.0 | Remaining quota across Claude Code profiles, Codex and Antigravity, and where to spend it: MCP server, skill, CLI/TUI, macOS menu bar app. |
| [`bailout`](plugins/bailout/skills/bailout/README.md) | 1.1.0 | Hands a task off before the five-hour Claude usage window runs out, and ends a `/goal` loop once the handoff exists. Install its hooks from a checkout: `plugins/bailout/install.sh`. |

agent-executor 0.10.0 adds a native `claude` engine, keeps hosted models off OpenRouter, reads routes from `~/.config/agent-executor/config.json` (`routes`, `init`, `doctor`), and installs from a checkout with `plugins/agent-executor/install.sh`, which links every harness to one copy. Routing calibration is still deferred; model profiles remain provisional. See the [checkpoint and resume notes](plugins/agent-executor/skills/agent-executor/references/design-completion.md).

```text
/plugin marketplace add loftshed/claude-whisperer
/plugin install agent-executor@claude-whisperer
/plugin install ai-usage@claude-whisperer
```

ai-usage also has a CLI, a menu bar app and MCP registrations for Codex and agy, installed from a checkout: see [its README](plugins/ai-usage/README.md#install).
