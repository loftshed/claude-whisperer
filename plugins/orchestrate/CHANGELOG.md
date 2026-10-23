# Changelog

## Unreleased

- docs(orchestrate): add plugin README covering installation, CLI entry points, `consult`, bounded delegation, `peer-mailbox`, team succession, and `stooges` rooms

## 0.30.0

- feat(orchestrate): propose a deputy from quota facts and take over on standing evidence (#21)

## 0.29.2

- fix(orchestrate): detect blind credits watch and map Gemini account home (#20)

## 0.29.1

- fix(orchestrate): report the released version in doctor and run records (#19)

## 0.29.0

- feat(orchestrate): carry a cleared session's requests and tell senders about ended ones (#18)

## 0.28.0

- feat(orchestrate): one accountable team lead with fenced, verified succession (#17)

## 0.27.0

- feat(orchestrate)!: stop Codex runs from spending credits unless allowed (#16)

## 0.26.0

- feat(orchestrate): name peers person-repo-model-effort (#15)

## 0.25.0

- feat(orchestrate): route mailbox by account, hand native work to the host, add ork rooms (#14)

## 0.24.2

- fix(orchestrate): harden cross-harness wakes, mailbox ergonomics, and model routing (#12)

## 0.24.1

- fix(orchestrate): receive explicitly addressed cross-project mail (#10)

## 0.24.0

- feat(orchestrate): refine stooges TUI framing and document task archetype rationale
- docs: clarify native messaging with external coordinators
- feat: migrate to orkest and finish orchestration tooling
- Add task archetype routing telemetry

## 0.23.1

- fix(orchestrate): clarify peer labels and compact communication commands

## 0.23.0

- feat(orchestrate): show task purpose, model and harness in peer discovery

## 0.22.0

- Add `run_agent.py consult`: one focused question to a more capable model, with inline context and
  text attachments, answered once with the model that answered and its stated uncertainty. The
  consultant has no write permission (Codex read-only sandbox, Claude Code without tools or MCP
  servers, Antigravity plan mode, OpenCode on a private server with its tools disabled; each
  checked live with a write probe). `--intent investigate` and `--intent
review` with `--read <repo>` (and `--diff`) reuse the same mechanism with read tools in one
  repository, returning findings with evidence; a changed repository is reported, not judged.
  Explicit models, effort and Claude profiles are kept, and pinned across `--follow-up` with the
  intent and repository; a model the calling harness runs itself is handed back to its native agent
  tools; `--detach` resolves the model first and publishes a completion event. Attachments named
  or located like credentials are refused on the given and the resolved path. Implementation
  delegation keeps its separate scoped, audited and verified workflow.

## 0.21.0

- feat!: rename ai-usage to monitor, round remaining days, and add icon-only bar mode

## 0.20.0

- Harden runner process lifecycle, report extraction, quota lane matching, and model registries:
  stop leaking worktree lock file descriptors to child processes, reap killed process groups, drop
  shared `refs/stash` from worktree history checks, recover valid reports from post-report Claude
  turns and sticky Antigravity error frames, match `--claude-config-dir` to its quota pool and
  scrub inherited auth overrides, invalidate cached CLI versions on binary updates, and split
  report contracts, output parsers, and completion events into focused modules.

- Add one authenticated loopback HTTP mailbox daemon with stable lifecycle commands and endpoint.
  Bind callers explicitly, keep HTTP mail unread until acknowledgement, and recover keyed sends
  without duplicate delivery or activation. Install protected HTTP configuration for all four hosts,
  preserve unrelated settings/comments, and provide explicit stdio rollback. Add crash, concurrency,
  transport, installation, native Codex and read-only evaluation checks.

- Support MCP `2026-07-28` discovery and per-request identity while retaining legacy clients.
  Add output schemas, complete annotations, enforced input bounds and paginated mailbox reads.
  Keep message bodies once in default Markdown output, shorten repeated guidance, cache modern
  tool discovery, and bound pending waits. Add protocol, response-budget and read-only evaluations.

- Add persistent Claude and Gemini/Antigravity wake adapters with `run_agent.py listen`.
  Queue mailbox notifications between completed turns in one conversation, preserve project scope,
  and keep quiet delivery and unread mail intact.

## 0.19.0

- Agents can report communication problems and what they needed through the local `feedback` tool.
  Automatic diagnostics capture routing errors, reply timeouts, withheld context and extended turns
  without copying message bodies. Reports deduplicate, write to a configurable collector, and never
  message or wake another agent. `feedback-summary` groups observations and recent agent needs.

## 0.18.0

- Discovery, sending and incoming context default to the same project, including sibling worktrees and
  clones sharing a Git remote. Explicit `scope: "cross-project"` or CLI `--scope cross-project` enables
  coordination across projects for one call; automatic hooks stay within the project.
- Claude Code and Codex hooks announce other live sessions in the project with branch and declared
  focus at session start and when that set changes. `focus` lets each session say what it is working on.
- Wake idle Codex daemon and OpenCode v2 peers when sending mail; defer busy sessions without
  interruption, support quiet delivery, and report unsupported or unavailable activation explicitly.

- Install and repair Linux peer messaging from the skill itself: register each installed harness,
  preserve existing settings, install delivery hooks, apply scoped Codex mailbox permissions, and
  verify message/reply round trips without model quota. Codex hook trust remains limited to the
  four reviewed mailbox commands.

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
