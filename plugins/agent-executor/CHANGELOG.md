# Changelog

## Unreleased

- Run messaging now uses the mailbox: `messages` is replaced by `mailbox`, and `AGENT_CHANNEL` by
  `AGENT_MAILBOX`; runs started before this upgrade keep no live channel.

## 0.15.0

- A machine-wide peer mailbox lets Claude Code, Codex, Antigravity and OpenCode sessions list each
  other and exchange messages: the `peer-mailbox` MCP server (`peers`, `send`, `inbox`, `wait`, `ack`),
  `run_agent.py mailbox`, and turn hooks for Claude Code and Codex that deliver mail at each prompt.
  It carries only cross-harness traffic: same-harness peers use native messaging, and Claude to Claude
  is refused. Messages arrive wrapped as `<peer-message>`, marked as coming from an agent, not the user.
  Setup: `references/mailbox.md`.

## 0.14.0

- Routes and `--model` take family selectors (`gpt:luna`, `gemini:fast`, `claude:best`, `glm:flash`)
  that resolve to the newest live model; `run_agent.py families` shows the resolutions.
- The launch gate, `quota` table, blocked-route alternatives and `coordinator.py recommend` treat an
  ai-usage pool with status `credits` (Codex once its weekly allowance is used up, with a credit balance)
  as usable: runs go ahead metered against the balance and print `AGENT_LIFECYCLE=quota_credits`; the
  pool ranks after routes with free quota. Exit 15 now means exhausted with no credits.

## 0.13.0

- feat(agent-executor): add OpenCode v2 support with version detection and caching
