# Changelog

## 0.17.0

- Agents see who else works in the same repository: the Claude Code and Codex hooks list other live
  sessions in it (any harness, any worktree) with branch and declared focus at session start and when that
  set changes, `peers` puts them first, and a new `focus` tool lets each session say what it is working on.

## 0.16.1

- The peer mailbox is hardened against races, stale or damaged state, and edge cases a host can hit:
  - **Mail delivery:** concurrent readers never get the same message, and a cancelled `wait` leaves its
    mail unread.
  - **Runs:** a send cannot land after a run's result, and `events wait --messages` reports a run that
    closed without a completion event and follows steered jobs.
  - **Identity:**
    - session ids that differ only by `:`/`_` or by case keep separate files, and older names migrate;
    - a run cannot take over a live session with the same name;
    - output directories with spaces or a shared basename get their own runs;
    - each OpenCode session gets its own identity.
  - **Liveness:** a live session stays listed however long it is idle, and a reused pid no longer keeps
    a dead one.
  - **MCP server:** it validates JSON-RPC requests and tool arguments, answers batches, and honours
    cancellation.

## 0.16.0

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
