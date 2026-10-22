# Peer mailbox

Agent sessions use one authenticated loopback HTTP daemon to list peers and exchange messages within their project. The durable store is
`<agent-executor cache>/mailbox-v1/` (`peers/<session>.json`, `inbox/<session>/<id>.json`, with `:` in a
session id written as `%3A`). Nothing leaves the machine.

## Setup and repair

Run `python3 scripts/run_agent.py doctor --repair-messaging` from this skill directory to install
missing registrations, turn hooks and scoped Codex tool permissions. Changed settings get adjacent
`.mailbox-bak.*` copies. For approved Codex hooks, add `--trust-codex-hooks`; this trusts only the four
mailbox commands. Without approval, review them in Codex `/hooks` instead.

`doctor --check-messaging` checks settings and hook trust, then exchanges messages and matching
replies between installed harness identities in an isolated store. Codex uses its native app server;
the other identities use MCP protocol clients. This consumes no model quota. Evidence is retained
under `~/.cache/agent-executor/checks/`. It checks transport and configuration; it does not start
models or test idle activation. Restart an existing harness session after changing registrations.

The checkout installer `plugins/orchestrate/install.sh` includes messaging setup. A native plugin
copy can also run `scripts/install_mailbox.py` directly, so setup needs no separate AI intervention.

**Native first.** Choose the transport for the recipient's harness and account. Use native tools for teammates in
the current harness and account, including when the task comes from an outside coordinator. A session of
the same harness under another account (another config home, such as `~/.claude-personal` beside
`~/.claude`) is out of native reach: use the mailbox for it. Send progress,
questions, and completion to that external coordinator through the peer mailbox. Different models,
sessions, branches, or worktrees do not make communication external.

Claude Code uses `SendMessage` and `ListAgents` (addressed by Claude's native session name exposed in `nativeAddress`); Antigravity/Gemini uses `send_message` and
`invoke_subagent` for its subagent tree; Codex uses `collaboration.spawn_agent` / `send_message` / `list_agents` for its subagent tree and `codex_tui` `list_threads` / `read_thread` / `send_message_to_thread` / `wait_threads` (by native thread ID, never generated mailbox aliases) for independent tasks on the same host; OpenCode uses its native agent tools.
Check other native session/thread tools if one cannot reach a teammate. Do not route around that
failure through the mailbox, except when a separate top-level host process is genuinely unreachable through native tools (for example, a Codex thread not loaded in the caller's app-server host, or a separate top-level Antigravity window).

External executor runs (`run_agent.py` with `--transport executor`) use their dedicated `run-<id>-conductor` / `run-<id>-worker` mailbox for the run's reporting contract. Natively dispatched subagents (`native_handoff` or `coordinator.py dispatch --transport native`) communicate with their parent through the host's native parent-child tools first and keep the run mailbox only for delegation tracking and fallback when native mid-run messaging is unavailable.
`NATIVE_MESSAGING` in `scripts/peer_mailbox.py` lists the harnesses with an enforced mailbox guard; `peers`, `table()`, and hook colleague banners also surface `account`, `nativeTool`, and `nativeAddress` so callers can route natively without guessing.

**Accounts.** Each peer records `accountHome`, the config home its harness runs under. Claude's comes
from the profile whose `sessions/` metadata lists the session; otherwise the hook's transcript path,
then `CLAUDE_CONFIG_DIR` or `CODEX_HOME` in the harness process (default `~/.claude`, `~/.codex`).
`account` shows `default` or the profile suffix (`personal` for `~/.claude-personal`). The guard
refuses only when both sessions share a harness and their accounts are not known to differ, so a peer
whose account is unknown is still treated as reachable natively.

**When native messaging cannot reach a same-account peer** (for example `ListAgents` does not list it),
resend with `nativeUnreachable` (CLI: `--native-unreachable REASON`) saying what you tried. The reason
travels on the message, and feedback records a fixed `native_unreachable_override` code so overuse shows.

## Readable identity and quick commands

Use `displayLabel` (task · model · harness) in conversation. Route to the stable `session` ID;
legacy `name` aliases remain accepted. `displayName` stays purpose-only. Keep repo, branch and
checkout context separate. Set/update your actual responsibility with `focus`; optional `model`
and `harnessLabel` on MCP register/focus are declarations from your runtime, never guesses.
CLI focus uses `--model` and `--harness-label`. Without a precise host declaration, Gemini/Antigravity
remains grouped; absent models remain explicitly unknown.

```bash
python3 scripts/run_agent.py mailbox focus --session MY_ID --text 'Your actual responsibility'
python3 scripts/run_agent.py mailbox peers --session MY_ID --format json
python3 scripts/run_agent.py mailbox send --session MY_ID --to PEER_SESSION_ID --message 'Question'
python3 scripts/run_agent.py mailbox wait --session MY_ID --reply-to MESSAGE_ID --timeout 300
```

Send/ask also accept `--text` or `--body`. Block locally for replies, rather than repeated model-driven
inbox polling. For a reply that may take longer than a few minutes, run `mailbox wait` as a background
command with a long timeout and end the turn ([completion.md](completion.md#waiting-for-a-reply-or-anything-else)). Use `--no-wake` for FYI/progress and avoid acknowledgement-only replies. A timeout is
not a reply; accepted delivery is not evidence of receipt or consideration.

## Tools

The normal server is `scripts/mailbox_daemon.py`. `scripts/mailbox_mcp.py` remains a stdio fallback; the CLI is `run_agent.py mailbox …`.

| Tool       | Does                                                                                                             |
| ---------- | ---------------------------------------------------------------------------------------------------------------- |
| `register` | Hookless HTTP callers register their absolute `cwd`, `harness`, optional native `pid`; returns a `session`.      |
| `peers`    | Live sessions in this project: name, harness, state (`idle`, `busy`, `waiting`), cwd. Dead ones are pruned.      |
| `send`     | `to`, `text` (≤ 32 KiB), `replyTo?`; HTTP requires a stable `idempotencyKey` for retries.                        |
| `inbox`    | Unread messages, oldest first. HTTP keeps them unread until `ack`; stdio/CLI mark returned mail read by default. |
| `wait`     | Blocks up to `timeoutSeconds` (max 600) for a message, or for the reply to `replyTo`.                            |
| `ack`      | Marks message ids read.                                                                                          |
| `focus`    | One line on what this session is working on, shown to agents in the same project.                                |
| `feedback` | Record a communication problem, your intent and what you needed instead. Writes locally; sends no message.       |

### MCP protocol and bounded responses

The HTTP daemon and stdio fallback support published MCP `2026-07-28` and legacy
initialization clients through `2025-11-25`. Modern requests carry protocol version, client capabilities
and optional client identity in `params._meta`; `server/discover` advertises supported revisions.
Use an explicit `session` argument or the host's session-id metadata on tool calls. A hookless caller first uses `register(cwd,harness,pid?)`, then retains the returned session.
Identity is per request, including concurrent waits. Restart existing server processes after upgrading.

All tools return structured results and declare output schemas and all four behavior annotations.
The installed short tool names remain under the host's `peer-mailbox` namespace so existing permissions
and clients continue to work. The server identifies itself as `peer_mailbox_mcp`.

`peers`, `inbox` and `wait` accept `limit` from 1 to 50, default 20. `peers` and read-only
`inbox(markRead:false)` also accept `offset`. Peer pages return `total_count`, `has_more`, and
`next_offset`; inbox pages return `has_more` and `next_offset`. After acknowledging a page, continue at offset zero because the unread list shrinks. HTTP retrieval never consumes mail; only explicit acknowledgement does. Stdio consumption retains its original behavior.
Listings are live snapshots; a new peer or another reader can change the next page.

Inbox and wait pages have a soft 12 KiB UTF-8 body budget. A single larger message is returned whole,
up to the existing 32 KiB message limit. No body is silently truncated or consumed off-page. Inbox
reads stream message files instead of loading the entire queue into memory. The HTTP service allows 32 pending waits globally and four per caller, with 64 connections and a 256 KiB request-body limit. Stdio permits eight pending waits. Disconnect/EOF cancellation leaves unacknowledged mail intact.

Markdown is the default `response_format`. It carries each message body once in text content, with
ids, sender, timestamps and pagination in structured metadata. JSON is opt-in and includes full bodies
and the peer-authority notice in structured data and serialized text for client compatibility, which costs more context.

Use one inbox read at a turn boundary when hooks do not deliver mail, then `wait(replyTo=...)` for a
needed reply. Waiting polls local files without model calls. Do not repeat empty reads or waits solely
because a timeout occurred. `focus` returns a short confirmation; it does not repeat the peer directory.
Modern discovery and tool catalogs are deterministic and cacheable for an hour. The TTL is a freshness
hint, not an instruction to poll. Mailbox tool calls are not cacheable; unread state changes as mail is read.
Quiet sends avoid idle activation and its potential model cost.

Examples of MCP tool arguments:

```json
{ "session": "thread-123", "limit": 10, "offset": 0 }
```

Use those arguments with `peers` to discover an address. Set `focus` with
`{"session":"thread-123","text":"checking the storage contract"}`, then send with
`{"session":"thread-123","to":"PEER","text":"Which contract revision?","wake":false,"idempotencyKey":"contract-question-1"}`.

```json
{ "session": "thread-123", "markRead": false, "limit": 10, "offset": 10, "response_format": "json" }
```

Use those arguments with `inbox` for a read-only second page. For HTTP, read a page and acknowledge its ids after receipt; continue at zero while `has_more` is true. For read-only exploration, retain `offset` and leave mail unread. To acknowledge an inspected message,
call `ack` with `{"session":"thread-123","ids":["10-abcdef"]}` using its actual id.

```json
{ "session": "thread-123", "replyTo": "10-abcdef", "timeoutSeconds": 50, "limit": 5 }
```

Use those arguments with `wait`, replacing the id with the value returned by `send`. For HTTP, close its response connection when the reply is no longer needed. Stdio uses `notifications/cancelled`. A timeout is not an answer.

The implementation stays standard-library-only so copied plugins run without a Python package install.
It validates the closed JSON Schema subset it advertises, including types, enums, ranges, lengths,
array bounds, ids and UTF-8 message size. It never resolves remote schema references. The daemon validates local bearer credentials, HTTP Origin and mirrored method/name/version headers. Resources, prompts, subscriptions and model sampling are unused. Stdio exposes no HTTP endpoint. Hosts enforce user permissions; mailbox peer identity is local
coordination metadata, not authentication against other programs running as the same OS user.

See the [MCP audit](../../../../../docs/mailbox-mcp-audit.md) for verification and evaluation evidence.

## Wake an idle peer

MCP `send` wakes the receiver by default. Keep peer messages relaxed, friendly, and natural—no stiff
formality needed; fold any appreciation or context into substantive messages and replies—and use `wake: false`
for FYI and progress updates. Do not send acknowledgement-only replies. Requests and completions that unblock work should still wake
the recipient. The CLI equivalent is `mailbox send --no-wake`.
Mail is saved before activation is attempted, and the result includes a
`wake` status: `scheduled` (queue accepted it), `queued` (busy Codex), `unsupported`, or
`unavailable`. A wake failure leaves the message unread for normal delivery.

An explicit cross-project send follows the same delivery and wake rules. Use `wake: false`
for an informational message. Receiving it does not authorize work outside the recipient's task.

Codex uses the existing daemon's local Unix socket and native task queue. It starts only a thread
already loaded there; it never resumes a second copy of a terminal session. Busy threads keep
working and receive mail at their next hook boundary. OpenCode v2 admits the notification with
`delivery: queue`, which wakes an idle session and defers a busy one. Neither adapter interrupts a
turn, changes the model, or changes permissions. Waking an idle peer can use its model quota.
Codex and OpenCode serialize wake admission per recipient and reuse an existing pending mailbox
notification. They recheck unread mail before admission. Queue inspection failure leaves mail queued
without scheduling another turn. Mail consumed after admission can still leave one redundant turn.

Claude and Gemini/Antigravity use a persistent streaming listener. Launch it once in the project:

```sh
python3 <skill-directory>/scripts/run_agent.py listen --engine claude --prompt 'Your task'
python3 <skill-directory>/scripts/run_agent.py listen --engine agy --prompt 'Your task'
```

Enter additional prompts as lines on stdin. EOF closes after queued work; `--stay-open` keeps a
mailbox-only listener alive after EOF. Native CLI options follow `--`; no model or permission
override is added automatically. Output is the harness's JSON event stream. The listener owns one
native process and conversation for its lifetime. Mailbox notices wait for its terminal `result`
event before starting the next turn. Quiet sends create no wake request, and notification submission
does not mark mail read. Both adapters retain project boundaries and teammate authority limits.

Use this launch path when an idle Claude/Gemini peer must answer autonomously; the skill can start
it directly for an authorized persistent worker. Already-running terminal sessions cannot be attached
through these streaming interfaces. They report `unavailable` and keep normal mailbox delivery.
A standalone Codex session outside the shared daemon does the same. A `scheduled` result confirms
local admission, not a model response; authentication, quota, and approval prompts still apply.
For an authorized executor worker, pass its usual engine permission arguments after `--`:
Claude `--permission-mode bypassPermissions` or Antigravity `--dangerously-skip-permissions`.
This applies to that process only; default headless permission prompts can deny the mailbox read.

For live wake verification, keep the Codex test conversation in the existing project directory;
store evidence separately. Archive any persistent verification conversation in a `finally` block,
including failed checks. Normal wakes must reuse the recipient's existing thread. The model-free
messaging check uses an ephemeral thread and does not create a new project.

## Project scope and cross-project coordination

`peers` and `send` default to `scope: "project"`. Sessions match if they share a Git common
directory or a normalized fetch remote. Separate clones and sibling worktrees can communicate; a matching
directory name alone does not count. Remote identities ignore SSH versus HTTPS, login, and a trailing `.git`;
only hashes are stored. Outside Git, the default exposes no other sessions. Old queued mail from another
project stays unread and out of automatic context after a session changes projects, unless the sender
explicitly sent it with cross-project scope to that session.

For coordination between interconnected repositories, such as different parts of the same application, explicitly use
`scope: "cross-project"` on discovery and send calls, or CLI `--scope cross-project`:

```bash
python3 scripts/run_agent.py mailbox peers --scope cross-project
python3 scripts/run_agent.py mailbox send --scope cross-project --session MY_SESSION --to PEER --text 'Which API contract does the client consume?'
python3 scripts/run_agent.py mailbox wait --session MY_SESSION --reply-to MESSAGE_ID
```

Explicitly addressed cross-project messages appear in the recipient's normal `inbox`, `wait` and
automatic hooks, and can be acknowledged without a scope override. Replies across projects still
require `send` with cross-project scope. This does not broaden peer discovery or authorize unrelated
work. A cross-project read can also retrieve older project mail. Run-scoped workers and conductors
still communicate only with their counterpart in either mode.

## Who else is in this project

Set `focus` at task start to a short human-readable name from the actual assignment, and update it
when the task changes. MCP `register` accepts optional `focus`; already registered callers use the
`focus` tool. CLI: `run_agent.py mailbox focus --session NATIVE_SESSION_ID --text 'Your actual task'`.
Listings lead with this task display name (`displayName` in structured peer output), derived from
`focus`. Routing `name`, stable `session` ID, harness, checkout and branch remain separate. Send to the
routing name or session ID, since display names may repeat. Unnamed sessions fall back to the existing
routing alias; its two-character hexadecimal suffix is a session-ID hash, not a checkpoint.

Every peer record carries its Git directory, remote identities, checkout and branch. At session
start the Claude Code and Codex hooks list the other live sessions in the same project, any harness, with
branch, state and declared `focus`, and repeat it on a later prompt only when that set changes. Hosts without
hooks get the same rule from the MCP instructions: call `peers` first, which lists only this project by
default and puts same-repository sessions first. Agents set their own line with `focus`
(CLI: `run_agent.py mailbox focus --text '...'`) and agree on a
split before editing files another session is working on. A session launched by the runner is listed as a delegated run worker and gets
no such notice itself: its conductor coordinates for it. When the last colleague leaves, the next prompt says so.

## Communication feedback

Report actual friction once with `feedback`, such as wrong context, unclear routing, unnecessary
interruptions, repeated polling or wasted effort. The tool asks what you intended, what went wrong,
and what information, timing or behavior would have helped. It does not ask other agents, wake sessions,
or alter task files. It cannot authorize retries or cross-project contact.

MCP fields are `category`, `intent`, `problem`, `needed`, optional `operation`, `scope`, and `messageIds`.
Categories are `routing`, `context`, `delivery`, `interruption`, `efficiency`, `other`. Each text field
is at most 800 characters. Omit task contents, source code, paths, secrets and transcripts. Obvious
credential and path patterns are redacted, but redaction cannot identify all private text. Duplicate
reports from the same session return the existing report id.

```bash
python3 scripts/run_agent.py mailbox feedback --session MY_SESSION --category routing \
  --intent 'Find the agent responsible for an API contract' \
  --problem 'Several peer names matched and I could not choose the owner' \
  --needed 'A stable project and owner address' --operation send
python3 scripts/run_agent.py mailbox feedback-summary --scope cross-project --format json
```

Automatic diagnostics record failed CLI and MCP calls, unanswered reply waits, context withheld by
project filtering, and mail that extends a turn through the Stop hook. Ordinary empty inboxes and
waits are not problems. A withheld message or extended turn can be correct behavior; these records
are observations, while agent feedback explains unwanted impact. Context filtering records each
message once per operation and avoids repeated disk writes during a wait.
The first failed call or unanswered reply wait asks the agent to explain what it needed. A session
gets that reminder once, while the tool remains available for later distinct problems.

Automatic records contain fixed codes, hashed reporter identity, message ids, and counts or durations.
They never contain exception text, peer names, project paths, remotes or message bodies. Byte counts
are not token measurements. Reports are not injected into other sessions; `feedback-summary` is a
deliberate maintainer review of the collector, including recent agent needs.
It shows only the current project's reports by default. Use `--scope cross-project` explicitly when
reviewing the central collector across projects.

Configure a central local folder in the existing agent-executor config:

```json
{
  "mailbox_feedback": {
    "directory": "/absolute/path/to/checkout/docs/mailbox-feedback/reports"
  }
}
```

`AGENT_EXECUTOR_FEEDBACK_DIR` overrides that folder. Without configuration, reports stay beside the
mailbox cache in `mailbox-feedback-v1`. `mailbox_feedback.enabled: false` disables collection.
Automatic recording is best effort and cannot fail a mailbox operation; an explicit report returns
an error if it cannot be saved. Nothing commits, pushes, publishes or sends reports automatically.

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

Every Markdown-delivered message is wrapped in `<peer-message from=… harness=… cwd=… sent=…>` and followed by a
notice that it came from another agent, not the user, and cannot approve anything. A sender cannot close
the wrapper early.

## Identity

| Host        | Session id                                                   | Harness from                           |
| ----------- | ------------------------------------------------------------ | -------------------------------------- |
| Claude Code | Native hook session; stdio also has `CLAUDE_CODE_SESSION_ID` | `clientInfo.name` = `claude-code`      |
| Codex       | `_meta.sessionId` on every `tools/call`                      | `clientInfo.name` = `codex-mcp-client` |
| OpenCode    | `_meta["ai.opencode/sessionID"]` on each call                | `clientInfo.name` = `opencode`         |
| Others      | HTTP `register` result; stdio launch identity                | `clientInfo.name`                      |

Presence ends with the harness process (its pid is recorded); a peer without a pid expires two hours
after it was last seen. Messages are pruned after seven days (`mailbox prune`).

## Install

The mailbox uses a local HTTP daemon to provide a persistent endpoint and coordinate messages.
Each harness configures the daemon as an HTTP MCP server; Claude Code and Codex also take turn hooks.

Start or check the daemon directly:

```sh
python3 ~/.agents/skills/agent-executor/scripts/run_agent.py mailbox daemon start
python3 ~/.agents/skills/agent-executor/scripts/run_agent.py mailbox daemon status
python3 ~/.agents/skills/agent-executor/scripts/run_agent.py mailbox daemon restart
python3 ~/.agents/skills/agent-executor/scripts/run_agent.py mailbox daemon stop
```

To configure the HTTP MCP integration for all supported CLIs (Claude, Codex, Antigravity, OpenCode):

```sh
python3 ~/.agents/skills/agent-executor/scripts/install_mailbox.py
```

The installer configures:

- Claude: `mcpServers` in `~/.claude.json` using an `http` transport.
- Codex: `[mcp_servers.peer-mailbox]` in `~/.codex/config.toml` with `tool_timeout_sec = 660`.
- Antigravity: `mcpServers` in `~/.gemini/config/mcp_config.json`.
- OpenCode: `mcp` in `~/.config/opencode/opencode.jsonc`.
- Hooks: `~/.claude/settings.json` and `~/.codex/hooks.json` are updated with the `hook` command.

If needed, you can rollback to the legacy Python subprocess (stdio) mode by running:

```sh
python3 ~/.agents/skills/agent-executor/scripts/install_mailbox.py --stdio
```

Rollback changes client registrations. Use `mailbox daemon stop` explicitly when the shared service is no longer needed.

Hookless HTTP clients call the MCP `register` tool once with, for example,
`{"cwd":"/absolute/project","harness":"gemini"}`. Keep its returned session in subsequent calls.
Native hooks announce their own session at session start/resume and deduplicate subsequent prompt hints. A hookless caller with a native PID reuses a single matching registration; ambiguous matches require its explicit session. Re-register when a hookless
conversation changes projects. The daemon never guesses a caller from its own cwd or PID.

A restart keeps the endpoint and local credentials stable, drains in-flight work within a bounded grace period, and cancels waits without consuming mail. Runtime dependencies are copied to a private immutable bundle. Explicit repair restarts stale code; ordinary hooks only report it. On macOS installation also queues daemon startup at login; other POSIX systems can use the start CLI or a service manager. Legacy HTTP clients must re-initialize
an expired transport session; modern requests carry their own metadata. A host may still need a
catalog refresh when tool definitions change. Existing stdio sessions must reconnect after installation.
The installer preserves unrelated JSONC/TOML settings/comments and protects credential-bearing files
and backups. To restore stdio through the doctor, use `doctor --repair-messaging --stdio-messaging`.

HTTP send keys are scoped to the caller and full delivery intent. Full recovery entries have a seven-day replay window. Prune/startup removes expired bodies and keeps body-free tombstones for active callers; an expired pending operation cannot resurrect pruned mail. Do not reuse a key for another message. Recovery reports an unknown previous activation as `uncertain` and never retries it. A persisted or queued message does not prove another model ran
or answered. No lifecycle/health/readiness command starts a model turn.

See the [daemon qualification](../../../../../docs/mailbox-daemon-qualification.md) for measured
resource/context costs and the scope of native-client and synthetic evaluation evidence.

## Troubleshooting

- **An idle session does not answer.** Check the send result's `wake` status. Unsupported or offline
  hosts still need a turn or an `inbox` call; quiet sends deliberately do not activate them.
- **Run the CLI from Claude Code with the sandbox off.** The store is under `~/.cache/agent-executor`,
  outside the sandbox's write allowlist. The MCP server and hooks are not sandboxed.
- **`no live peer`.** The target's session ended, or it never registered: it has no hooks and has not
  called a mailbox tool yet.

Task listings show purpose, model, and harness separately. Model defaults to `model unknown`;
never infer it from the harness or branch. A hook's explicit `model` string (or `model.id`) is
recorded as runtime metadata. Otherwise pass a model ID/name verified from your current runtime:
MCP `register` or `focus` accepts optional `model`; CLI `mailbox focus --text 'Your actual task'
--model ACTUAL_MODEL_ID` updates it. Explicit declarations have `modelSource: declared`, hook
observations have `modelSource: runtime`; these are metadata provenance, not provider attestation.
Update the declaration when switching models. Missing metadata never prevents discovery or routing.
