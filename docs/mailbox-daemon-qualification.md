# Mailbox daemon qualification

The normal implementation is one authenticated loopback HTTP MCP daemon. The original stdio server
remains an explicit fallback. The existing durable store and project/run/native-first boundaries remain.
Implementation was delegated to Gemini 3.1 Pro High; conductor review rejected incomplete handoffs and
added contract hardening and behavior tests. Personal Opus 5.5 reviewed the combined tree without edits. Its confirmed findings are corrected and receive dedicated regressions before final handoff.

## Verified behavior

- Ten concurrent starters share one daemon PID and endpoint; ten callers and reused RPC ids remain distinct.
- Native hook context and explicit hookless registration bind session, cwd, harness and caller lifetime.
  Missing, expired and mismatched context fails without adopting daemon identity.
- HTTP retrieval is non-destructive. Explicit scoped acknowledgements consume only received messages.
- Stable caller/delivery keys recover from a process crash after delivery/activation but before response,
  without another message or wake. An uncertain previous activation is reported as uncertain rather than queued. Recipient departure does not invalidate a committed replay.
- Recovery keys expire after seven days. Startup/manual prune removes expired bodies; active callers retain body-free key tombstones so expired pending operations cannot resurrect pruned mail. Malformed recovery files remain available for inspection and do not prevent startup.
- Restart retains endpoint and atomically published owner-only credentials. Health reports live runtime identity. Stale state,
  reserved/busy endpoints and unauthorized shutdown do not trigger signals to unrelated processes.
- Authentication, exact Origin parsing, bounded bodies/deadlines/connections/waits, header mirroring,
  isolated legacy sessions and disconnect cancellation are exercised through actual HTTP.
- Four host configuration formats preserve unrelated settings/comments, use protected headers/files and
  backups, remove stdio-only fields on migration, and restore stdio explicitly.
- Hooks continue durable local delivery if the daemon is unavailable, skip startup on Stop, and refresh identity on resume. Lifecycle CLI and activated hooks use no model calls. Native identity guidance is short and deduplicated.
- Official Inspector 2.9.0 passes modern list/call and legacy list checks with no portability findings.
- Ten actual HTTP responses and all eight output schemas pass the official MCP 2026-07-28 JSON Schema.
- A native disposable Codex app-server discovers/registers/calls the HTTP server without model generation.
  Twelve model-free message/reply exchanges cover four harness identities, including native Codex.
- The ten frozen evaluation answers remain retrievable through HTTP and leave messages unread.

The [current HTTP specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
and [versioning rules](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning) are the
protocol reference. HTTP supports current and 2025 initialization clients. The older 2024 HTTP+SSE
transport is unused; 2024 clients retain the stdio fallback.

## Measured workload

One local run used ten callers and 25 send/read/ack cycles with the same synthetic message text and
current protocol. HTTP additionally records durable keyed intent. These measurements are not a
production latency distribution or tokenizer measurement.

| Measurement                              |       Ten stdio servers | One HTTP daemon |
| ---------------------------------------- | ----------------------: | --------------: |
| Warm resident memory                     |               253.8 MiB |        38.2 MiB |
| Median cold caller setup                 |               124.48 ms |        26.89 ms |
| Service startup                          | Included per subprocess |  123.58 ms once |
| Median send/read/ack cycle               |               153.42 ms |        55.05 ms |
| Cycle p95 in this run                    |               159.52 ms |        58.19 ms |
| Mean model-facing result bytes per cycle |                   1,446 |           1,452 |

Idle memory snapshots were much smaller because pages were reclaimed. The warm workload is the useful
comparison. An earlier trial measured 253.3/31.0 MiB and 124.87/150.64 ms median cycles, with HTTP slower.
The repeat above demonstrates a smaller working set and variable latency. It does not establish a universal speed gain. Default Markdown carries each body
once; JSON duplication is an explicit compatibility choice. Waiting polls locally without model calls.

## Reproduce

From the repository root:

```sh
TMPDIR=/private/tmp python3 -B -m unittest discover -s tests/plugins/orchestrate/skills/orchestrate -p 'test_*.py'
python3 plugins/orchestrate/skills/orchestrate/scripts/run_agent.py mailbox daemon --help
```

The canonical macOS temp path avoids the existing `/var` versus `/private/var` installer comparison.
The full plugin suite passes 406 tests. Focused regressions cover headerless legacy batches, startup lock contention, malformed recovery files, named OpenCode servers, scoped TOML settings and committed batch responses during shutdown.
On macOS the installer writes an owner-only login definition that runs the daemon directly and retains the configured PATH and optional CODEX_HOME. Login and explicit startup use a private immutable runtime bundle. A disposable native launchd job successfully started the service on port 65001 and was removed after qualification, with no model generation. Other POSIX systems can use the documented start CLI or a local service manager.

The tests own all services they interrupt and use disposable stores/unreserved ports. No live harness
registrations or existing agent processes are changed by qualification. Four-host identity simulation
and native Codex checks do not prove every native host version or idle model activation; the separate
wake suite covers adapter behavior.
The OS user remains the trust boundary; caller identity is coordination metadata. Runtime bundles and protected configuration backups are retained for recovery. Concurrent configuration checks detect observed changes but cannot coordinate every external writer. Qualification does not prove every custom host environment or automatic idle model activation.
