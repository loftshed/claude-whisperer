# Claude Whisperer

Personal Claude Code plugin containing the `agent-executor` and `bailout` skills.

The plugin manifest and all skills live at the repository root:

```text
.claude-plugin/plugin.json
skills/agent-executor/
skills/bailout/
```

Load the plugin from a checkout with:

```bash
claude --plugin-dir /path/to/claude-whisperer
```

Routing calibration is deferred to conserve usage; model profiles remain provisional. See the [checkpoint and resume notes](skills/agent-executor/references/design-completion.md).
