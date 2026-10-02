# Runner behavior

Detail behind the SKILL.md core. Read it when a result surprises you, when choosing effort or
billing, or when changing the runner.

## Engines

| Engine     | Invocation                                                                                                                                                                                                   | Brief                                                        | Permissions                                               |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------ | --------------------------------------------------------- |
| `codex`    | `codex exec` JSONL plus `--output-last-message`                                                                                                                                                              | stdin                                                        | `--dangerously-bypass-approvals-and-sandbox`              |
| `agy`      | `agy -p` stream-json output, terminal presentation disabled                                                                                                                                                  | stdin (one stream-json message; agy ≥ 1.1.15, else argument) | `--dangerously-skip-permissions`                          |
| `claude`   | `claude -p --output-format json` (Claude Code), a fresh run named with `--session-id <uuid>`; the report is the last `result` message, read from one object, a verbose JSON array, JSON lines or stream-json | stdin                                                        | `--permission-mode bypassPermissions`, `--add-dir <repo>` |
| `opencode` | `opencode run --pure --auto`, all-allow agent overrides, no sharing or auto-update                                                                                                                           | file attachment                                              | inline overrides                                          |

Every executor gets an environment scrubbed of the host session (`CLAUDECODE`, `CLAUDE_CODE_*`,
`CLAUDE_CONFIG_DIR`, `AI_AGENT`), so a Claude host's tokens and profile never leak into a child.
`--engine claude` runs Claude Code's default profile unless `--claude-config-dir` names another; the
profile decides the billing account, so follow the user's rules.

OpenCode cannot add workspace roots faithfully; use Codex, agy or claude when a task needs `--add-dir`.

## Model catalogs

`models` reads a private cache at `<cache>/models-v1.json`. A cached entry is valid for 24 hours
while its CLI version is unchanged; a miss, version change, or requested model missing from the entry
triggers a refresh. `--refresh` / `--refresh-models` force it. Codex is listed through `app-server`
`model/list`, agy through `agy models`, OpenCode through `opencode models --pure`. Claude Code has no
listing command, so its catalog is the aliases `opus`, `sonnet`, `haiku`, `fable`; a full `claude-*`
ID is accepted unverified. None of these prompt a model. Always filter:
`models --engine agy --match gemini-3.8`; an unfiltered listing costs thousands of tokens.

`--model` also takes a family selector (`gpt:sol`, `gemini:fast`, `deepseek`) from
[families.json](families.json), resolved against the live catalog to the newest matching ID; a selector
alone infers its engine. The result records the resolved ID with `model_preflight:
live_family_resolved`, `families` prints every resolution, and `AGENT_NOTE=model_advanced` on stderr
marks a selector that now resolves to a newer model. A coordinator dispatch records the resolved ID,
and `--resume-result` keeps a selector on the model the session ran.

## Effort and variants

Pass `--effort` for reproducible runs. Codex effort is validated against the model's catalog and bound
as `model_reasoning_effort`; claude takes `low|medium|high|xhigh|max`; agy effort must match the
catalog slug (`gemini-3.8-flash-high`); agy rejects `--effort` alongside a slug, and no current
agy model offers `max`. OpenCode keeps its provider `--variant`; never translate
effort names across providers. Results keep requested, command-bound, and provider-observed settings
separately; unknown observed settings stay null.

## Usage and reference cost

`usage` keeps raw counters and exclusive normalized buckets. Codex snapshots are cumulative, so a
resumed run without a matching prior result has unknown incremental usage. OpenCode records reasoning
separately and deduplicates steps. agy keeps cumulative and step counters; its billing semantics are
unknown. claude reports input, cache read/creation and output tokens plus Claude Code's own cost
estimate (`native_cost_usd`). Unknown is never zero.

`--billing-mode api|subscription|unknown` declares the billing context without inspecting credentials.
[The dated registry](model-registry.json) holds verified public API tariffs; a reference estimate is
emitted only when usage, rate freshness (30 days, or an entry's `revalidate_on`) and tariff tier permit.
agy slugs use their base model's tariff. It is not an invoice. See
[verification findings](research-verification-2026-09-18.md).

## Audit and classification

- The runner holds a POSIX worktree lease through execution and verification. Empty-scope runs
  share reader leases; a writer excludes other readers and writers (exit 27). Stdin task inputs
  conservatively retain an exclusive lease. The lease does not stop editors.
- History is judged on this worktree only: branch, HEAD, HEAD reflog, stash. A change there is a
  `history_violation` (21). Commits, branches or worktrees created elsewhere in the repository are
  reported in `concurrent_activity` and do not fail the job.
- Scope compares the final-state path delta with the allowed prefixes (22).
- Verification commands run through `/bin/sh` after a clean handoff, one `--verify-timeout` each,
  fail fast, keep bounded tails and private logs. The repository is re-audited afterwards (25/26).
- The report needs the five headings in order. Markdown decoration is tolerated (`**STATUS**`,
  `## STATUS:`, `` `COMPLETE` ``, `COMPLETE.`); a qualified `COMPLETE - except …` is malformed.
- A provider terminal error is surfaced in `provider_error` even when the CLI exits 0: agy's result
  `error`, Claude Code's `is_error` result, Codex's `error`/`turn.failed` event, OpenCode's `error` event.
- A failed, empty or malformed run becomes `provider_quota_exhausted` (16) only on provider evidence:
  an anchored quota, credit or rate-limit message (`RESOURCE_EXHAUSTED`, `429 Too Many Requests`,
  `insufficient_quota`, `usage limit reached`, …) in stderr or that terminal error, or a structured
  signal (Claude `rate_limit_event` with `status: rejected`, an assistant `error` of `rate_limit` or
  `billing_error`, `api_error_status: 429`; OpenCode `statusCode: 429`). Stdout content and tool output
  are never searched, so a task about quotas or rate limits cannot trip it. The same evidence decides
  `--retry-read-only` eligibility.
- After a nonzero executor exit the catalog is refreshed to record whether the model still exists.
  A possibly mutating run is never retried; `--retry-read-only <1-3>` retries only unchanged,
  empty-scope transient failures and keeps every attempt.

Final-state checks cannot prove that no transient or external side effect occurred.

Every new run also has a [live message channel](communication.md). Questions and replies use
ordinary CLI tool calls inside the existing executor session; provider text streaming is not
required. This does not expose internal reasoning or automatically record every native host message.

## Steering a running job

`steer <job-or-event> --message <text|@file> [--cwd <repo>] [--wait-timeout 2m] [--format json]`
redirects a detached job. The job is named by its directory, its `job.json`, or any of its event IDs
or `AGENT_EVENT` paths.

The handoff happens inside the job's own runner, the process that holds the worktree lease, so the
lease never changes hands. `steer` writes the message to `<job>/steers/<n>/message.txt` and a request
to `<job>/steer-request.json`. While the executor runs, the runner polls for the request every 0.5 s
and claims it with an atomic rename, so a request is either claimed or withdrawn by `steer`, never
both. It answers in `response.json` before it touches the executor. When it accepts, it:

1. Sends SIGINT to the executor's process group, then SIGTERM after 10 s, then SIGKILL after another
   10 s, and waits for the executor to exit. SIGINT is what a user's Ctrl-C sends, so each CLI saves
   its session as it would for a person.
2. Closes the interrupted segment. Its `result.json` gets `status: steered` along with the delta at
   the interrupt. Its event is published already acknowledged, with `superseded_by`, so the inbox
   shows one result to review and `events wait`/`follow` on the old ID follow the job.
3. Resumes the same session, with a new turn made of the message plus the unchanged scope and checks
   and the full report contract. It keeps the engine, model, effort, variant, billing, timeouts,
   deadline, notification and completion hook. Artifacts go to `<job>/steers/<n>/run/` and the new
   event ID is returned.

The final result is judged against the snapshot taken before the first segment, so history, scope,
`--expect-changes` and the verification gates cover the combined run. `result.steering` lists every
accepted steer. `<job>/steering.json` records the runner's phase and every request, rejected ones
included. Each segment gets the full `--timeout`, the same as a retry or a `--session` correction;
`--deadline` still bounds the whole job. The completion hook and desktop notification fire only for
the final result.

| Engine     | Session ID source while running                                                  | Resume                        |
| ---------- | -------------------------------------------------------------------------------- | ----------------------------- |
| `codex`    | `thread.started` (first JSONL line)                                              | `codex exec resume <id>`      |
| `claude`   | assigned up front with `--session-id` (json output prints nothing until the end) | `claude -p --resume <id>`     |
| `agy`      | stream-json `init.conversation_id`, or the log's "Created conversation"          | `agy --conversation <id>`     |
| `opencode` | `sessionID` on the first event                                                   | `opencode run --session <id>` |

Refusals come before any request is written:

- Exit 30: the job finished, or its executor already handed off to verification. The message gives
  the exact `--session <id> --resume-result <result.json>` correction.
- Exit 31: `--cwd` names another worktree.
- Exit 32: the runner PID is gone, or it belongs to another command, which is checked against the
  job's event path in `ps` output so a reused PID is refused.
- Exit 33: the job predates 0.11.0, or another steer is in progress.

Two runtime rejections also exit 33, and in both the executor keeps running untouched:

- The engine has not printed a session ID yet. Retry shortly. If none ever appears, the only fallback
  is to stop the job and relaunch with the instruction in the brief, and the executor's context is lost.
- The runner did not take the request within `--wait-timeout`, for example between retry attempts.
  The request is withdrawn.

Usage accounting across segments follows the resume rules above. An interrupted segment may have no
usage record, and in that case it is reported unknown, not zero.

## State and retention

Foreground artifacts go to `<cache>/runs-v1`, detached ones to `<cache>/jobs-v1`. `prune` removes old
runs, jobs and acknowledged events (`--older-than 14d`, `--dry-run`); it never removes an
unacknowledged event or the job it points to. Normal launches prune anything older than 30 days at
most once a day.

## Routes and setup

`routes` prints the effective role → engine/model/effort table: the bundled
[defaults](routes.default.json) overlaid by `~/.config/agent-executor/config.json`
(`$AGENT_EXECUTOR_CONFIG`, or under `$XDG_CONFIG_HOME`). `init` writes a starter config. `doctor`
reports the host, installed CLI versions, skill installs, and stale copies.
