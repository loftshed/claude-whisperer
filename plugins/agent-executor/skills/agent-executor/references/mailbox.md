# Peer mailbox

Agent sessions in different harnesses list each other and exchange messages through a local store,
`<agent-executor cache>/mailbox-v1/` (`peers/<session>.json`, `inbox/<session>/<id>.json`). Nothing
leaves the machine.

**Native first.** A session messages agents of its own harness with that harness's native tools whenever
it has any (Claude Code: `SendMessage` and `ListAgents`). The mailbox is only for crossing harnesses, and
it refuses Claude Code to Claude Code outright. Harnesses known to have native messaging are listed in
`NATIVE_MESSAGING` in `scripts/peer_mailbox.py`; add one there when another harness gains it.

## Tools

The MCP server (`scripts/mailbox_mcp.py`) and the CLI (`run_agent.py mailbox …`) share the names.

| Tool    | Does                                                                                         |
| ------- | -------------------------------------------------------------------------------------------- |
| `peers` | Live sessions: name, harness, state (`idle`, `busy`, `waiting`), cwd. Dead ones are pruned.  |
| `send`  | `to` (name, session id, or a unique prefix of 6+ characters), `text` (≤ 32 KiB), `replyTo?`. |
| `inbox` | Unread messages, oldest first, marked read unless `markRead: false`.                         |
| `wait`  | Blocks up to `timeoutSeconds` (max 600) for a message, or for the reply to `replyTo`.        |
| `ack`   | Marks message ids read.                                                                      |

Every delivered message is wrapped in `<peer-message from=… harness=… cwd=… sent=…>` and followed by a
notice that it came from another agent, not the user, and cannot approve anything. A sender cannot close
the wrapper early.

## Identity

| Host        | Session id                                   | Harness from                           |
| ----------- | -------------------------------------------- | -------------------------------------- |
| Claude Code | `CLAUDE_CODE_SESSION_ID` in the server's env | `clientInfo.name` = `claude-code`      |
| Codex       | `_meta.sessionId` on every `tools/call`      | `clientInfo.name` = `codex-mcp-client` |
| Others      | hash of harness, harness pid and cwd         | `clientInfo.name`                      |

Presence ends with the harness process (its pid is recorded); a peer without a pid expires two hours
after it was last seen. Messages are pruned after seven days (`mailbox prune`).

## Install

Each harness needs the MCP server; Claude Code and Codex also take the turn hooks, which register the
session, deliver mail at each prompt, keep a turn going when mail arrived during it (`Stop`), and remove
the session at `SessionEnd`.

```sh
claude mcp add --scope user peer-mailbox -- python3 ~/.agents/skills/agent-executor/scripts/mailbox_mcp.py
codex mcp add peer-mailbox -- python3 ~/.agents/skills/agent-executor/scripts/mailbox_mcp.py
agy mcp add peer-mailbox python3 ~/.agents/skills/agent-executor/scripts/mailbox_mcp.py
```

- Codex: add `tool_timeout_sec = 660` under `[mcp_servers.peer-mailbox]` in `~/.codex/config.toml`, or
  `wait` is cut off at Codex's 60-second default. Codex tools appear as `mcp__peer-mailbox__send` and so on.
- Hooks: in `~/.claude/settings.json` and `~/.codex/hooks.json` (same format), one command hook each for
  `SessionStart`, `UserPromptSubmit`, `Stop` and `SessionEnd`:
  `python3 ~/.agents/skills/agent-executor/scripts/peer_mailbox.py hook --harness claude` (or `codex`).
- OpenCode: a `local` entry under `mcp` in `~/.config/opencode/opencode.jsonc`.

## Troubleshooting

- **An idle session does not answer.** Mail is pulled at turn boundaries; nothing wakes an idle session.
  Hosts without hooks (Antigravity, OpenCode) see mail only when the agent calls `inbox`.
- **Run the CLI from Claude Code with the sandbox off.** The store is under `~/.cache/agent-executor`,
  outside the sandbox's write allowlist. The MCP server and hooks are not sandboxed.
- **`no live peer`.** The target's session ended, or it never registered: it has no hooks and has not
  called a mailbox tool yet.
