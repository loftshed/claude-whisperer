# Single mailbox daemon plan

## Outcome

Run one local HTTP MCP daemon for all agent hosts. Keep the existing mailbox store, project boundaries,
message wrappers, feedback, wake behavior and bounded responses. Direct HTTP registrations replace
per-host Python mailbox servers after qualification. Stdio remains a compatibility fallback, not the
normal daemon client path.

Use published MCP `2026-07-28` and preserve supported legacy clients. Modern calls are self-contained;
legacy HTTP initialization must isolate its client context. Do not add runtime dependencies: this
plugin currently runs using the Python standard library from a copied installation.

## Service contract

Add `scripts/mailbox_daemon.py` with `serve`, `start`, `status`, `stop` and `restart` commands. Commands
accept `--root` for an isolated mailbox store, optional `--port`, and `--json` for safe machine output.
The importable controller API is `ensure_running(base: Path, port: int = 0) -> dict`,
`load_state(base: Path) -> dict | None`, and `read_token(base: Path) -> str`. Public state includes
`url`, `pid`, `instance` and implementation/protocol version information, never the bearer token.
State and token files live in a private sibling `mailbox-daemon-v1` directory beside `mailbox-v1`.
Use separate startup and service-lifetime locks so simultaneous starters produce one owner.

Bind only `127.0.0.1`. First startup may select an available ephemeral port; persist and reuse that
port across restart so configured client URLs remain valid. Never bind the developer's reserved
ports. Never stop an unrelated listener or a PID based only on a stale state file. An authenticated
health response and service instance identity must establish ownership before shutdown.

Expose `/mcp` for MCP and authenticated `/health` for operational verification. Prefer a bounded,
graceful authenticated shutdown over signals. Cancel pending waits on shutdown, finish committed
operations, release the service lock, then restart on the same URL. Start the service detached from
an individual agent host and scrub inherited session/delegation/auth-provider environment.

Support bounded request bodies, connection/request deadlines, per-caller wait limits and a global
limit sufficient for at least ten simultaneous callers. Discard completed request bookkeeping.
Logs must omit authorization headers, task payloads and credentials. Local credentials/state must
be owner-only. Validate HTTP Origin, method/name/version headers, authentication and content type;
implement the current spec's POST, notification, error and disconnect cancellation semantics.
Do not advertise unused subscriptions, sampling, resources or task extensions.

## Caller identity

Never derive an HTTP caller's project, session or lifetime from the daemon's cwd, environment or PID.
Native hooks already register Claude and Codex identities. Reuse a verified live record addressed by
explicit `session` or host session-id metadata. Preserve the hook-owned cwd, native PID, harness,
run/delegation fields and project identity. Reject an unknown/missing caller with a next step.

For hookless clients, add one bounded `register` tool that accepts a caller's cwd, harness and optional
native PID/session, creates or refreshes presence, and returns the session to pass on subsequent calls.
Keep required context in one registration rather than every message result. Never conflate different
harnesses, sessions, worktrees or projects on one shared transport. A locally authenticated OS user
is the trust boundary; peer metadata never authorizes wider tasks or cross-project delivery.

## Delivery and restart correctness

HTTP inbox and wait retrieval must be non-destructive by default. Consume messages only through an
explicit acknowledgement after receipt. Keep a clear distinction between delivered, acknowledged,
queued wake and actual peer response. Make discovery descriptions/instructions match these defaults.
A killed request or service must leave unacknowledged messages retrievable, including after restart.
Preserve existing stdio/hook compatibility and the durable file store.

HTTP send must require a bounded application idempotency key, and the same key/payload must replay
the same committed message id without sending or waking again. Reusing a key with a different payload
must fail. Persist the operation identity before side effects so a process crash between writes can
recover without a second message. Validate routing/project ownership before journal replay. Retain
an explicit replay window and do not imply exactly-once remote model execution or reply.

Test interruption after durable message creation but before the HTTP response, retry after restart,
and unread retrieval cancelled or disconnected before acknowledgement. Include concurrent readers,
per-caller ordering, isolated request ids, repeated acknowledgements and no duplicate wake attempts.

## Installation and operation

Extend installer/doctor tooling to configure direct authenticated HTTP for Claude profiles, Codex,
Antigravity and OpenCode, preserving unrelated configuration and making adjacent backups. Never put
credentials in argv, stdout, logs, committed files or reports. Protect configuration files containing
headers. Keep Codex tool permissions scoped to mailbox operations and preserve hook trust policy.
Provide an explicit stdio rollback path and diagnostics that distinguish an unavailable daemon,
stale client catalog, invalid identity and missing configuration.

Add `run_agent.py mailbox daemon start|status|stop|restart` or an equally discoverable documented CLI
entry. Hooks may ensure service availability in daemon mode, but must not start a new model turn or
restart a healthy service on every prompt. Preserve direct CLI/run mailbox use and existing wakes.
Daemon startup, health, local waits and restart itself make no model calls.

Do not mutate live registrations or stop existing user/agent processes during implementation. Qualify
with temporary stores and disposable host clients first. A live rollout is a separate, concrete step
following implementation and both requested reviews.

## Work allocation

1. Gemini 3.1 Pro High implements the daemon, caller binding, HTTP semantics and durable delivery,
   with subprocess/concurrency/restart/security tests. It owns the daemon, shared mailbox core and
   protocol helpers. No installer or live configuration changes.
2. Gemini 3.1 Pro High implements direct HTTP installation, operational CLI, hook identity guidance,
   diagnostics/rollback and documentation against the established daemon API. It owns integration
   files and their tests. Use a separate worktree if running concurrently; otherwise keep the writer
   lease exclusive and run this phase after the backend handoff.
3. The conductor reviews both full diffs and requirement evidence, integrates, and fixes understood
   gaps. Then Opus 5.5 through the personal `plaude` wrapper performs a read-only review of the final
   implementation. Preserve all findings and verify accepted corrections.

## Acceptance evidence

- Concurrent startups and ten clients use one daemon PID and persistent endpoint.
- Multi-host/project/worktree identities remain distinct; forged/missing/expired context is rejected.
- Default project and explicit cross-project boundaries, native-first messaging and run restrictions hold.
- Abrupt and graceful restart preserve unacknowledged mail; keyed send retries do not duplicate messages/wakes.
- Disconnect cancellation, bounded waits/requests, authentication and Origin defenses are exercised.
- Modern and legacy HTTP clients discover/call tools; default and JSON output conform to declared schemas.
- All four installer formats use direct HTTP with protected headers, preserve unrelated configuration and roll back.
- Existing stdio, hooks, feedback, wake adapters, run coordination and evaluation workflows still pass.
- Compare cold/warm latency, CPU/memory and response bytes on the same model-free workload. Report measurements,
  not an assumed universal speed or token improvement; default message bodies remain single-copy.
- Run focused daemon/integration tests, the plugin suite, appropriate style/manifest checks, MCP Inspector and
  the existing ten read-only evaluations against the daemon path. Preserve the macOS canonical-temp workaround.
- Conductor and personal Opus review completed, findings addressed, and original checkout hashes unchanged.
