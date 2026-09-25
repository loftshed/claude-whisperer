# Claude Whisperer

Private personal plugin marketplace and versioned skill backup.

| Plugin | Version | What it does |
| --- | --- | --- |
| `agent-executor` | 0.9.0 | Bounded agent execution, consultation, evidence, and audit across Codex, Antigravity and OpenCode. |
| [`ai-usage`](plugins/ai-usage/README.md) | 0.7.1 | Remaining quota across Claude Code profiles, Codex and Antigravity, and where to spend it: MCP server, skill, CLI/TUI, macOS menu bar app. |

agent-executor 0.9.0 is a tested working checkpoint. Routing calibration is deferred to conserve usage; model profiles remain provisional. See the [checkpoint and resume notes](plugins/agent-executor/skills/agent-executor/references/design-completion.md).

```text
/plugin marketplace add loftshed/claude-whisperer
/plugin install agent-executor@claude-whisperer
/plugin install ai-usage@claude-whisperer
```

ai-usage also has a CLI, a menu bar app and MCP registrations for Codex and agy, installed from a checkout: see [its README](plugins/ai-usage/README.md#install).
