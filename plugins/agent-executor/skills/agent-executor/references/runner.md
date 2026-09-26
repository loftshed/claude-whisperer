# Runner behavior

Detail behind the SKILL.md core. Read it when a result surprises you, when choosing effort or
billing, or when changing the runner.

## Engines

| Engine | Invocation | Brief | Permissions |
| --- | --- | --- | --- |
| `codex` | `codex exec` JSONL plus `--output-last-message` | stdin | `--dangerously-bypass-approvals-and-sandbox` |
| `agy` | `agy -p` structured output, terminal presentation disabled | argument | `--dangerously-skip-permissions` |
| `claude` | `claude -p --output-format json` (Claude Code) | stdin | `--permission-mode bypassPermissions`, `--add-dir <repo>` |
| `opencode` | `opencode run --pure --auto`, all-allow agent overrides, no sharing or auto-update | file attachment | inline overrides |

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

## Effort and variants

Pass `--effort` for reproducible runs. Codex effort is validated against the model's catalog and bound
as `model_reasoning_effort`; claude takes `low|medium|high|xhigh|max`; agy effort must match the
catalog slug (`gemini-3.8-flash-high`). OpenCode keeps its provider `--variant`; never translate
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
emitted only when usage, rate freshness and tariff tier permit. It is not an invoice. See
[verification findings](research-verification-2026-09-18.md).

## Audit and classification

- The runner holds a POSIX worktree lease through execution and verification and rejects a second
  cooperating runner in that worktree (exit 27). The lease does not stop editors.
- History is judged on this worktree only: branch, HEAD, HEAD reflog, stash. A change there is a
  `history_violation` (21). Commits, branches or worktrees created elsewhere in the repository are
  reported in `concurrent_activity` and do not fail the job.
- Scope compares the final-state path delta with the allowed prefixes (22).
- Verification commands run through `/bin/sh` after a clean handoff, one `--verify-timeout` each,
  fail fast, keep bounded tails and private logs. The repository is re-audited afterwards (25/26).
- The report needs the five headings in order. Markdown decoration is tolerated (`**STATUS**`,
  `## STATUS:`, `` `COMPLETE` ``, `COMPLETE.`); a qualified `COMPLETE - except …` is malformed.
- A provider terminal error (for example agy `RESOURCE_EXHAUSTED`/429) is surfaced in
  `provider_error` even when the CLI exits 0; quota exhaustion becomes `provider_quota_exhausted` (16).
- After a nonzero executor exit the catalog is refreshed to record whether the model still exists.
  A possibly mutating run is never retried; `--retry-read-only <1-3>` retries only unchanged,
  empty-scope transient failures and keeps every attempt.

Final-state checks cannot prove that no transient or external side effect occurred.

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
