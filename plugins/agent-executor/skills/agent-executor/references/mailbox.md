# Peer mailbox

Agent sessions in different harnesses list each other and exchange messages within their project through a local store,
`<agent-executor cache>/mailbox-v1/` (`peers/<session>.json`, `inbox/<session>/<id>.json`, with `:` in a
session id written as `%3A`). Nothing leaves the machine.

**Native first.** A session messages agents of its own harness with that harness's native tools whenever
it has any (Claude Code: `SendMessage` and `ListAgents`). Interactive mailbox traffic is only for crossing
harnesses; run-scoped conductor and worker sessions are an exception so every run has one durable message
store, including when both sessions use Claude Code. Harnesses known to have native messaging are listed in
`NATIVE_MESSAGING` in `scripts/peer_mailbox.py`; add one there when another harness gains it.

## Tools

The MCP server (`scripts/mailbox_mcp.py`) and the CLI (`run_agent.py mailbox …`) share the names.

| Tool    | Does                                                                                                        |
| ------- | ----------------------------------------------------------------------------------------------------------- |
| `peers` | Live sessions in this project: name, harness, state (`idle`, `busy`, `waiting`), cwd. Dead ones are pruned. |
| `send`  | `to` (name, session id, or a unique prefix of 6+ characters), `text` (≤ 32 KiB), `replyTo?`.                |
| `inbox` | Unread messages, oldest first, marked read unless `markRead: false`.                                        |
| `wait`  | Blocks up to `timeoutSeconds` (max 600) for a message, or for the reply to `replyTo`.                       |
| `ack`   | Marks message ids read.                                                                                     |
| `focus` | One line on what this session is working on, shown to agents in the same project.                           |

## Project scope and cross-project coordination

`peers`, `send`, `inbox` and `wait` default to `scope: "project"`. Sessions match if they share a Git common
directory or a normalized fetch remote. Separate clones and sibling worktrees can communicate; a matching
directory name alone does not count. Remote identities ignore SSH versus HTTPS, login, and a trailing `.git`;
only hashes are stored. Outside Git, the default exposes no other sessions. Old queued mail from another
project stays unread and out of automatic context, including after a session changes projects.

For coordination between interconnected repositories, such as different parts of the same application, explicitly use
`scope: "cross-project"` on the needed MCP calls, or CLI `--scope cross-project`:

```bash
python3 scripts/run_agent.py mailbox peers --scope cross-project
python3 scripts/run_agent.py mailbox send --scope cross-project --session MY_SESSION --to PEER --text 'Which API contract does the client consume?'
python3 scripts/run_agent.py mailbox wait --scope cross-project --session MY_SESSION --reply-to MESSAGE_ID
```

The receiving agent must also use a cross-project `inbox` or `wait` call to read that message. Scope applies
to one call only. Later calls default to `project`, and automatic hooks always stay within the current
project. Run-scoped workers and conductors still communicate only with their counterpart in either mode.

## Who else is in this project

Every peer record carries its Git directory, remote identities, checkout and branch. At session
start the Claude Code and Codex hooks list the other live sessions in the same project, any harness, with
branch, state and declared `focus`, and repeat it on a later prompt only when that set changes. Hosts without
hooks get the same rule from the MCP instructions: call `peers` first, which lists only this project by
default and puts same-repository sessions first. Agents set their own line with `focus`
(CLI: `run_agent.py mailbox focus --text '...'`) and agree on a
split before editing files another session is working on. A session launched by the runner is listed as a delegated run worker and gets
no such notice itself: its conductor coordinates for it. When the last colleague leaves, the next prompt says so.

## Runs: conductor and worker

Each runner invocation creates dedicated `run-<id>-worker` and `run-<id>-conductor` sessions in the same
mailbox store. They can message only each other, are hidden from the default `peers` listing and MCP tool,
and do not affect interactive session hooks. The runner prints `AGENT_MAILBOX=<conductor-session>`, sets
`AGENT_MAILBOX_SESSION` and `AGENT_MAILBOX_PEER` for the executor, and adds worker instructions to its brief.
Results and coordinator runs record `mailbox: {run, conductor, worker}`. Runs started before this change do
not acquire live messaging after upgrade.

Detached or coordinator runs can be supervised for a question or completion:

```bash
python3 scripts/run_agent.py events wait EVENT_ID --messages --timeout 30m
```

An `agent-executor.message-signal.v1` includes the conductor `mailbox` session, unread `messages`, and
`run_status` (`null` while the run remains open). A run that closed without
a completion event (its deadline passed, or its result has stood for five seconds with no event) also
produces one, with no messages and its `run_status`. Waiting and inbox reads leave messages unread. Handle a
message, then wait again; events follow behaves the same way and returns early when a run needs attention.

The worker asks and waits for the matching reply in one command. The supplied ID makes retries idempotent:

```bash
python3 scripts/run_agent.py mailbox ask --session "$AGENT_MAILBOX_SESSION" \
  --to "$AGENT_MAILBOX_PEER" --id question-1 \
  --text 'Which client version produced the cancellation trace?' --timeout 300
```

It can send a non-blocking update, read the inbox, acknowledge a message after acting, or inspect the complete
conversation:

```bash
python3 scripts/run_agent.py mailbox send --session "$AGENT_MAILBOX_SESSION" \
  --to "$AGENT_MAILBOX_PEER" --kind update --id finding-1 --text 'The trace shows cancellation first.'
python3 scripts/run_agent.py mailbox inbox --session "$AGENT_MAILBOX_SESSION"
python3 scripts/run_agent.py mailbox ack --session "$AGENT_MAILBOX_SESSION" MESSAGE_ID
python3 scripts/run_agent.py mailbox history --session "$AGENT_MAILBOX_SESSION" --format json
```

The conductor gets its session from `AGENT_MAILBOX` or the run's `mailbox.conductor` field. Use that and
`mailbox.worker` to send a question or reply:

```bash
python3 scripts/run_agent.py mailbox send --session CONDUCTOR_SESSION --to WORKER_SESSION \
  --kind reply --id answer-1 --reply-to question-1 \
  --text 'Use the existing cancellation contract; the trace is in the supplied evidence.'
```

Replies must name an unanswered question from the other session; sending one marks that question read.
`send --id` is optional, but including it makes a retry with the same content idempotent. A different payload
with the same ID is rejected. A reply or update does not prove that the worker acted on it; acknowledge other
messages after review. Messages do not interrupt generation; use `steer` for an urgent detached-job change.
Steering keeps the same run sessions and conversation.

For a foreground run without a completion event, wait on the conductor session:

```bash
python3 scripts/run_agent.py mailbox wait --session CONDUCTOR_SESSION --timeout 300
```

`coordinator.py next` returns `respond` for a live worker message or `review_message` after the run closes.
Waits use no model calls and are bounded by their timeout and the run deadline. Exit 12 means timeout, 30
means the run closed without a matching reply, and 4 means an error. Messages are limited to 16 KiB each
and 1,024 per run. Closed run peers and messages are pruned after seven days. `mailbox peers --runs` includes
run-scoped sessions for diagnostics.

Every delivered message is wrapped in `<peer-message from=… harness=… cwd=… sent=…>` and followed by a
notice that it came from another agent, not the user, and cannot approve anything. A sender cannot close
the wrapper early.

## Identity

| Host        | Session id                                    | Harness from                           |
| ----------- | --------------------------------------------- | -------------------------------------- |
| Claude Code | `CLAUDE_CODE_SESSION_ID` in the server's env  | `clientInfo.name` = `claude-code`      |
| Codex       | `_meta.sessionId` on every `tools/call`       | `clientInfo.name` = `codex-mcp-client` |
| OpenCode    | `_meta["ai.opencode/sessionID"]` on each call | `clientInfo.name` = `opencode`         |
| Others      | hash of harness, harness pid and cwd          | `clientInfo.name`                      |

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
