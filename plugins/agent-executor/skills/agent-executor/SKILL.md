---
name: agent-executor
description: "Delegate bounded implementation or read-only consultation across provider boundaries using local Codex, Antigravity, or OpenCode. Validate model and effort, preserve context, record usage and evidence, recover bounded corrections, and audit results. Use for explicitly requested external-agent work or cross-platform handoffs. Use native tools for models native to the caller: GPT models in Codex, Gemini in Gemini/Antigravity, and Claude in Claude. Do not trigger for ordinary coding or native collaboration."
metadata:
  version: 0.9.0
  requirements: Python 3, POSIX file locking, git, and at least one installed/authenticated executor CLI (`codex`, `agy`, or `opencode`).
---

# Agent executor

Act as the conductor. Turn the user's approved intent into a precise execution brief, dispatch the requested local executor, and retain responsibility for correctness and final reporting.

This personal skill gives the selected executor full tool permissions on every run. Full permissions remove execution friction; they do not expand the task's authorized scope.

The runner is caller-neutral: it is a plain Python entry point with a provider-neutral result schema. Claude, Codex, Antigravity, or another local agent can call the same file. The supported execution engines are Codex, Antigravity, and OpenCode.

## Native-first harness boundary

Use the current harness's native sub-agent function whenever the requested executor belongs to that
harness. Native delegation preserves the harness's own model selection, reasoning controls,
workspace lifecycle, and completion delivery without adding a second orchestration layer.

Use agent-executor only when the task intentionally crosses a harness or provider boundary. Apply
the rule relative to the caller:

- A Codex host uses native sub-agents for Astra, Terra, Luna, Sol, Spark, and other GPT models. It
  may use agent-executor for Gemini/Antigravity or another genuinely external executor.
- A Gemini/Antigravity host uses its native mechanism for Gemini. It may use agent-executor for a
  non-Gemini executor that is not otherwise native to that host.
- A Claude host uses its native mechanism for Claude. It may use agent-executor for a non-Claude
  executor that is not otherwise native to that host.

Do not route through agent-executor merely because its runner technically supports the same model
family as the caller. Before model discovery or brief construction, identify the current harness
and stop if the requested executor is native; hand the task to the native sub-agent facility
instead.

## Choose ownership before choosing a model

Use three options. They do not require three separate agents.

| Situation | Action |
| --- | --- |
| Useful decisions, failed approaches, or coupled changes still live mainly in the current conversation | Continue in the current session |
| Another agent can finish from a portable packet, and the conductor can independently assess the outcome | Delegate if the benefit exceeds briefing, context transfer, and review cost |
| The owner understands the task but lacks one fact, diagnosis, or perspective | Consult narrowly and retain ownership |

A hard task can justify a strong model directly. A fresh investigator can challenge an anchored
hypothesis when given observations and alternatives. Gemini is eligible for investigation as well
as implementation. Model brands and an arbitrary minimum task duration do not decide ownership.
Keep small work local when a handoff would cost more than doing it.

Before implementation delegation, state the objective, exact scope, acceptance criteria, relevant
versions and conventions, decisions to preserve, failed attempts, and evidence pointers. Use the
optional task-spec `context_packet` for the last four. A bounded task limits authority and scope;
it does not prohibit retrieving more relevant context. Do not transfer private reasoning or opaque
native tool history across providers.

For consultation, state the decision needed and what evidence would change it. Read
[references/coordinator.md](references/coordinator.md) when a task needs consultation, correction,
Pointdexter evidence checks, or recovery across turns. Its CLI records dispatch intent before launch,
serializes task decisions, enforces a deadline and call limits, and never retries an uncertain dispatch.
The host agent still owns reasoning, native delegation, and semantic review.

Pointdexter is an evidence-checking role, usually a narrow consultation. Use it for uncertain API
behavior, version mismatches, disputed claims, repeated failures, or possibly stale skills. Inspect
installed versions and applicable skills, retrieve current official sources for those versions,
and return cited facts, inferences, gaps, and a discriminating test when documentation is insufficient.
Another agent's confidence does not verify a claim. A source hash proves artifact integrity, not truth.

Let a blocked executor exit and finish its audit before consulting. Resume the original exact
session with new evidence after conductor review. Keep the original blocked result intact.
Allow three consultations total, including at most one challenge, and one correction. At those
limits, the conductor investigates or replans. Ask the user only for missing intent, information,
or authority that belongs to them. Peer advice does not expand user authorization.

## Choose one completion owner

Keep waiting outside the language-model loop. Repeated status commands, sleeps, log tails, or conversational “is it done?” checks waste context even when each individual check is cheap.

Prefer these modes in order:

1. **Native host supervision:** Run the executor in foreground mode, without `--detach`, through the caller platform's background-command or task primitive. The host owns the process and delivers its normal completion signal. When Antigravity is the caller, let its command runner transition the foreground command to an asynchronous task and use its task/completion notification; do not nest the runner's detached mode underneath it merely to wait.
2. **Portable blocking signal:** When native lifecycle delivery is absent or cross-caller handoff matters, launch with `--detach`, retain the returned `AGENT_EVENT`, and make exactly one blocking wait call:

   ```bash
   python3 <skill-directory>/scripts/run_agent.py events wait \
     <exact-AGENT_EVENT-path-or-event-id> \
     --timeout 30m
   ```

   The Python process performs inexpensive filesystem checks internally and emits one compact JSON signal when the atomic terminal event appears. It does not invoke a model. Set the wait timeout beyond the executor timeout.

   If the caller's command tool yields while this process is still blocked, resume that same process with the longest host wait compatible with user-update requirements—normally about 60 seconds. A host continuation is not another executor poll. Do not launch a second `events wait`, inspect files, or ask a model for status.
3. **Multi-job blocking signal:** Supervise several known jobs with one process:

   ```bash
   python3 <skill-directory>/scripts/run_agent.py events follow \
     <first-AGENT_EVENT-path-or-id> \
     <second-AGENT_EVENT-path-or-id> \
     --timeout 1h
   ```

   `follow` deduplicates references, emits one flushed JSON line per completed job, and exits after every requested job is terminal. A timeout returns nonzero and names the unfinished event IDs.
4. **Later-turn inbox recovery:** If no process can remain attached, rely on desktop/hook delivery and query the shared inbox once when a later caller resumes.

`wait` and `follow` never acknowledge events. Inspect the referenced `result_path` and workspace delta first, then acknowledge explicitly.

If an `events follow` process is interrupted, do not relaunch the work. On the next turn, list the
repository inbox once, inspect every unacknowledged `result_path`, and acknowledge only after
review. The durable event remains unread until that acknowledgement.

## Consume detached completions on resume

Before starting another delegated run in a repository, query its shared local inbox once:

```bash
python3 <skill-directory>/scripts/run_agent.py events --cwd <absolute-repository-root> --format json
```

This is one local cache read, not polling. Every detached run keeps its private artifacts under `~/.cache/agent-executor/jobs-v1` and atomically publishes an event under `~/.cache/agent-executor/completions-v1`, so a job launched by one shell-capable agent can be collected by another. Do not run the list command repeatedly while a job is active; use one `wait`/`follow` process instead. Inspect each event's `result_path`, runner-owned verification evidence, and workspace delta, then acknowledge it:

```bash
python3 <skill-directory>/scripts/run_agent.py events --ack <exact-event-id>
```

Do not acknowledge an event before its result and workspace delta have been reviewed.

## Retrieve models before selecting

Never invent a model ID. Query the runner:

```bash
python3 <skill-directory>/scripts/run_agent.py models
python3 <skill-directory>/scripts/run_agent.py models --format json
python3 <skill-directory>/scripts/run_agent.py models --refresh
python3 <skill-directory>/scripts/run_agent.py models --engine agy --match gemini-3.8-flash
```

The default command uses a private catalog at `~/.cache/agent-executor/models-v1.json`. A cached platform entry is accepted for 24 hours only while its CLI version is unchanged. A cache miss, version change, or requested model absent from the cached entry triggers a refresh. `--refresh` forces every selected catalog live; `--refresh-models` does the same before execution.

Codex is enumerated through `app-server`'s `model/list`; Antigravity through `agy models`; OpenCode through `opencode models --pure`. These are catalog operations, not model prompts. Claude CLI currently has no safe enumeration command. Report its catalog as unsupported; never run `claude models` or invoke Claude merely to discover model names.
Use repeatable `--engine` to select platforms and repeatable `--match` for case-insensitive model-ID
substrings. Filtering happens after the cached/live catalog is normalized, so it does not prompt a
model or discard the full cached catalog.

## Select the execution profile

Use an explicit user request over every default, after applying the native-first harness boundary.
The routes below are valid only when they cross away from the current harness:

| Requested profile | Validated policy preference |
| --- | --- |
| No profile, bounded metered work | `--engine codex --model gpt-5.6-luna --effort medium` |
| Explicit Spark request | Discover first; report unavailability if no exact live ID exists |
| GPT models (e.g. GPT-5.6, GPT-5.6 Sol, GPT-5.5, GPT-5.4, GPT) | `--engine codex --model <matching-gpt-model>` |
| Antigravity, agy, or Gemini Flash | `--engine agy --model gemini-3.8-flash-medium --effort medium` |
| OpenCode GLM-5.2 or OpenCode without a model | `--engine opencode --model openrouter/z-ai/glm-5.2 --variant high` |
| OpenCode DeepSeek V4 Flash | `--engine opencode --model openrouter/deepseek/deepseek-v4-flash --variant high` |

*Note: For GPT models (e.g., `gpt-5.6-sol`, `gpt-5.6`, `gpt-5.5`), use `--engine codex` by default rather than OpenCode.*

These are cost-conscious preferences, not claims about availability. The runner exact-matches them against the catalog and stops with live suggestions if one disappears. Pass another exact model with `--model` when requested. Select only a CLI whose catalog exposes that exact ID.

## Effort, usage, and reference costs

Pass `--effort` explicitly for reproducible Codex runs. The runner validates it against that model's
live catalog and binds `model_reasoning_effort`. AGY effort must match the exact catalog slug.
OpenCode retains its provider-specific `--variant`; do not translate effort names across providers.
Native host modes such as `ultra` are supported only where the actual selected catalog advertises them.

Results retain requested model, command-bound model/effort, and provider-observed settings separately.
Unknown observed settings remain null. Omitted Codex effort leaves CLI defaults/configuration in control.

`usage` preserves raw counters and exclusive normalized buckets. Codex snapshots are cumulative;
resumed work without a matching prior result has unknown incremental usage. OpenCode records separate
reasoning and deduplicates step identities. AGY uses structured output and preserves actual cumulative
and step counters. Its exclusive billing semantics and separate-process resume scope remain unknown.
Unknown is never zero. Per-request context and cache class/TTL are recorded where exposed, otherwise null.
`--billing-mode` declares `api`, `subscription`, or `unknown` without inspecting credentials or switching
accounts. Change authentication profiles or billing context only with explicit user authorization.

[The dated registry](references/model-registry.json) contains the nine verified public API tariffs.
The runner emits a separate reference estimate only when usage, freshness, and tariff tier permit it.
It excludes tools, storage, service-tier adjustments, and unreported usage. It is not an invoice or
subscription cost. Native CLI estimates remain separate. Stale rates, unknown cache-write class,
and unresolved per-request long-context tiers produce unknown estimates. Refresh from official
sources before changing the verification timestamp. See [verification findings](references/research-verification-2026-09-18.md).

Treat the model roles in the supplied research as hypotheses. Compare current-owner work, a fresh
worker, and owner-plus-consultant on representative tasks. Record all attempts, acceptance, elapsed
time, usage gaps, human repair time, and quota pressure. Do not promote a route using agent self-ratings.

## Delegation boundary

Delegate work that clears the ownership decision above, including a bounded investigation or consultation with independently assessable evidence.

Keep these responsibilities with the conductor:

- Resolve scope and authority before dispatch; give an investigator the precise uncertainty to resolve.
- Identify the repository root, allowed paths, ignored deliverables, constraints, and real verification commands.
- Protect pre-existing user changes.
- Review the complete per-run delta and diff.
- Supply deterministic final-state gates as runner-owned verification commands.
- Decide whether the result is acceptable.

Do not delegate secrets, credentials, tokens, unrelated private files, or an unbounded goal with no assessable outcome. Brief content is sent to the selected model provider.

## Non-negotiable safeguards

1. For a handwritten brief, declare every file or directory prefix the executor may change with repeatable `--allow-path <repo-relative-path>`. For a JSON `--task-spec`, its normalized `allow_paths` are authoritative and the runner rejects duplicate path flags.
2. For a handwritten brief, declare every ignored or generated deliverable with `--track-path <repo-relative-path>`. For a JSON `--task-spec`, use its `track_paths`; tracked paths are fingerprinted and implicitly allowed.
3. Mutating runs use `--expect-changes`; the runner rejects this flag without enforced path scope.
4. The executor must not commit, push, merge, reset, clean, stash, checkout, switch branches, create worktrees, publish externally, or access secrets. The runner fails when final Git branch, HEAD, reflog, local refs, stash, or worktree identity changes.
5. The report must contain all five required headings exactly once and in order. Its first non-empty STATUS value must normalize to `COMPLETE`, `BLOCKED`, or `FAILED`. `BLOCKED` returns runner status `blocked` and exit 24; `FAILED` returns nonzero. Missing evidence headings or unknown status prose make the report malformed.
6. Pass deterministic final-state gates with repeatable `--verify-command`. They run outside the model after a clean handoff, fail fast, keep private logs, and are skipped when the executor already failed or reported a blocker. The exact command strings are retained in private artifacts; never embed secrets.
7. Verification commands must be non-mutating. The runner re-audits Git after them and returns `verification_mutation` if tracked final state changed, `verification_failed` if a command failed or timed out, and a safety status if scope or history changed.
8. A runner status of `completed` means the executor exited cleanly, reported `COMPLETE`, passed scope/history checks, made expected changes, and passed every requested runner-owned command. It is strong execution evidence, not automatic acceptance of architecture or the complete diff.

## Workflow

1. Inspect the repository and applicable instruction files. Record `git status --short`, current branch, and `HEAD` before delegation.
2. Produce a complete plan. Reconcile any user-supplied plan with the actual repository without silently changing its intent.
3. Read [references/brief-template.md](references/brief-template.md). Prefer its compact JSON-spec workflow: write only the task-specific values outside the repository, then pass the JSON directly through `--task-spec`. The runner checks that `repository_root` exactly matches `--cwd`, renders the complete standalone brief privately, and derives both path scopes from the same normalized spec. This keeps shared guardrails, report text, and repeated runner flags out of chat. Direct handwritten briefs remain supported when a spec is unsuitable. Give the executor focused development checks. Keep authoritative, deterministic final-state gates with the runner so their exit status is observed outside the model. The runner preflights any explicit `# Final response` section and rejects missing or reordered required headings before provider invocation. A brief without its own final-response section is valid because the runner appends the canonical contract.
4. Run the bundled runner from this skill's physical directory with the narrowest stable path prefixes.

Cross-platform Luna example, only from a non-Codex host:

```bash
python3 <skill-directory>/scripts/run_agent.py \
  --cwd <absolute-repository-root> \
  --task-spec <brief-spec.json> \
  --engine codex \
  --model gpt-5.6-luna \
  --effort medium \
  --verify-command 'python3 -m unittest tests.test_jobs' \
  --verify-command './node_modules/.bin/eslint src/jobs tests/jobs' \
  --verify-timeout 10m \
  --expect-changes
```

Portable detached example:

```bash
python3 <skill-directory>/scripts/run_agent.py \
  --cwd <absolute-repository-root> \
  --task-spec <brief-spec.json> \
  --engine agy \
  --model gemini-3.8-flash-medium \
  --effort medium \
  --verify-command 'python3 -m unittest tests.test_jobs' \
  --expect-changes \
  --notify desktop \
  --detach
```

Detached mode immediately returns `AGENT_JOB`, `AGENT_RESULT`, and `AGENT_EVENT` paths plus the exact `AGENT_EVENT_ID`. It writes one durable inbox event and sends a desktop notification by default; use `--notify none` only when silence is intentional. A trusted platform adapter outside every executor workspace may be supplied with `--completion-hook <absolute-executable>`; it receives the event path as its only argument plus `AGENT_EVENT`, `AGENT_RESULT`, `AGENT_STATUS`, `AGENT_OUTCOME`, `AGENT_VERIFICATION_STATUS`, `AGENT_ENGINE`, and `AGENT_MODEL` in a restricted environment.

After launch, either let the native host notify this caller or issue one `events wait` call against the exact returned event. If the host yields a still-running wait process, continue that process with long host-side waits; do not create successive model turns around short waits. Do not poll `events`, the result path, the process table, or raw logs.

Desktop notification and inbox delivery consume no model tokens. They do not universally resume an ended chat turn: no cross-vendor callback API exists. A completion hook can resume or message a host only when that host provides a real local callback. If same-turn review is mandatory and no such callback exists, use one blocking `events wait` process. Foreground executor mode is silent by default; set `--heartbeat-seconds 60` only when periodic progress output is operationally useful.
Foreground runs emit only coarse, process-derived `AGENT_LIFECYCLE` transitions for executor start,
executor finish with changed-file count, verification start, and verification finish. They do not
prompt the model. Antigravity is launched with terminal presentation disabled so title/control
noise stays out of host output while raw executor stdout, stderr, and logs remain private artifacts.

Omit `--expect-changes` and use an empty task-spec scope only for deliberately read-only analysis or validation; an empty allowed scope makes any final-state path change a scope violation. A bounded mutation is supported: supply non-empty spec scope plus `--expect-changes`. `--retry-read-only <1-3>` is an opt-in recovery tool only for empty-scope, read-only runs; it retries a detected transient provider failure only after Git state and history are unchanged, and retains every attempt artifact. OpenCode cannot establish additional workspace roots faithfully; use Codex or agy when a fresh task genuinely requires repeatable `--add-dir`.

The runner holds a POSIX worktree lease through execution and verification. It rejects another cooperating runner in that worktree. The lease does not stop editors or other tools. While a job is active, freeze that worktree: do not edit it, start another runner job in it, create a worktree, or change repository history. The audit intentionally cannot attribute concurrent edits safely, so it records them as scope or history violations. Serialize shared-worktree work; use pre-created isolated worktrees only when actual parallelism is worth their setup and merge cost.

5. Read `result.json` and the compact report. Treat `status` as the authoritative runner disposition, `task_outcome` as the normalized task result, and `reported_outcome` as the executor's parsed claim. Inspect `verification.status`, `run_delta`, `scope_violations`, `history_violations`, and every tracked deliverable. Start with `review`: it contains the result path, session ID, changed-file count, per-file added/removed lines when the path was clean before execution, runner-verification outcomes, scope/history violations, and lightweight review flags. A path dirty before execution is explicitly marked instead of presenting cumulative Git statistics as a per-run delta.
6. Inspect `git status` and the complete diff. Confirm that all modifications match the brief and pre-existing work remains intact.
7. Audit acceptance criteria using runner-owned evidence and direct review:
   - Exercise negative and missing-value paths at input boundaries.
   - Confirm existing behavior and documentation were preserved unless the plan changed them.
   - Check pagination, output bounds, deduplication, and retry behavior where applicable.
   - Confirm ignored deliverables and external-write guardrails explicitly.
   - Map every acceptance criterion to code, a test, or a direct observation.
8. A passed `--verify-command` is independent of the executor because the runner launched it after the model exited. Do not waste time rerunning an identical gate unless review found a reason to distrust its scope or final state. Run additional narrow checks when acceptance criteria were not covered.
9. If review fails, send one focused correction using the exact recorded `--session <id>`, engine, model, effort, variant, and path scope. Supply `--resume-result <previous-result.json>` to validate that identity and establish a usage baseline, or take the work back locally. Do not repeat an unchanged failed prompt. Avoid `--continue` unless the most recent session is unambiguously the intended one.

## Runner behavior

The runner:

- defaults to live-validated Luna or Gemini 3.8 Flash Medium preferences, with OpenCode preferences preserved;
- caches normalized catalogs for 24 hours, invalidates on CLI-version or requested-model mismatch, supports forced refresh, and exact-matches every execution model;
- refreshes after a nonzero executor exit to record whether the selected model still exists, never retries a possibly mutating run, and retries only explicitly requested, unchanged, empty-scope read-only transient failures;
- uses Codex JSONL plus `--output-last-message`, submits the brief through stdin, and passes `--dangerously-bypass-approvals-and-sandbox`;
- checks exact agy models, uses `--dangerously-skip-permissions`, and retains the Antigravity log;
- checks exact OpenCode models, uses `--variant high`, `--auto`, and inline global/build-agent all-allow permission overrides, disables auto-sharing and auto-updates, runs in `--pure` mode without external plugins, never passes `--share`, and reads the brief as a file attachment;
- retains raw stdout/stderr, the submitted brief, the final output, and structured evidence in a private run directory outside the repository by default;
- suppresses Antigravity terminal presentation noise, is otherwise silent while waiting by default, emits coarse process-derived lifecycle transitions, offers opt-in heartbeats, gives detached runs an atomic shared completion event plus default desktop notification, and provides blocking `events wait`/`events follow` consumers whose filesystem checks never invoke a model;
- preflights explicit brief report contracts before provider invocation and supports engine/model-substring catalog filtering without changing the full cache;
- enforces the complete ordered five-heading report, parses STATUS into `reported_outcome`, exposes a normalized `task_outcome`, and never turns a declared blocker into successful completion;
- runs repeatable non-mutating `--verify-command` values through `/bin/sh` after a clean executor result, applies one `--verify-timeout` per command, fails fast, and retains bounded tails plus private stdout/stderr paths;
- fingerprints and audits the repository again after verification so verifier changes, scope violations, and Git-history changes cannot hide behind passing exit codes;
- reports the exact final-state path delta and fails on out-of-scope changes;
- includes a compact review bundle with changed paths, trustworthy per-run line counts where available, verification outcomes, session/result references, and lightweight suspicious-symbol flags;
- records Git branch, HEAD, reflog, local refs, stash, and worktree identity before and after;
- requires the final response contract while preserving raw executor output.

Final-state checks cannot prove that no transient or external side effect occurred. The brief and conductor review remain mandatory. Repository-wide refs, stashes, and worktrees can also produce a safety-biased history violation when another process changes them concurrently.

## Completion report

Use this compact form:

```text
Agent executor: completed | blocked | failed | timed out | no changes | scope violation | history violation | malformed report | verification failed | verification mutation
Task outcome: complete | blocked | failed
Engine: <codex | agy | opencode>
Model: <exact model>
Variant: <value or n/a>
Run artifacts: <result.json path>
Changes: <concise file summary>
Runner-owned verification: <commands and observed outcomes>
Remaining risks: <none or concise list>
```

Never present delegation as successful when the runner failed, reported a blocker, expected changes were absent, the report was malformed, scope/history checks failed, the diff exceeded scope, or requested runner-owned verification did not pass.

## Skill development gate

After changing this skill, renderer, or runner, run:

```bash
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
```
