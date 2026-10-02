---
name: agent-executor
description: "Delegate bounded implementation or read-only consultation across provider boundaries using local Codex, Antigravity, Claude Code, or OpenCode. Validate model and effort, check live quota, preserve context, record usage and evidence, recover bounded corrections, and audit results. Use for explicitly requested external-agent work or cross-platform handoffs, for example 'ask GPT-5.6 Sol', 'have Gemini check this', 'try it with GLM'. Use native tools for models native to the caller: GPT models in Codex, Gemini in Gemini/Antigravity, and Claude in Claude. Do not trigger for ordinary coding or native collaboration."
metadata:
  version: 0.14.0
  requirements: Python 3, POSIX file locking, git, and at least one installed/authenticated executor CLI (`codex`, `agy`, `claude`, or `opencode`).
---

# Agent executor

Act as the conductor. Turn the user's approved intent into a precise brief, dispatch the requested
local executor, and keep responsibility for correctness and the final report. Every executor runs with
full tool permissions; that removes friction, it does not widen the task's authorized scope.

`<skill-directory>` below is this file's physical directory. Start with
`python3 <skill-directory>/scripts/run_agent.py doctor` on an unfamiliar machine: it names the host,
the installed CLIs, the routes in effect, and stale skill copies.

## Native first; OpenRouter only for open-weight models

Use the host's own sub-agent facility when the requested model is native to it: GPT in Codex, Gemini
in Gemini/Antigravity, Claude in Claude Code, OpenCode models in OpenCode. Use agent-executor only to
cross a harness or provider boundary.

Every hosted frontier model goes through its own vendor's harness, never through OpenCode/OpenRouter:

| Model family                                                    | Engine                                                                   |
| --------------------------------------------------------------- | ------------------------------------------------------------------------ |
| GPT (Astra, Sol, Terra, Luna, GPT-5.x/6)                        | `--engine codex`                                                         |
| Gemini                                                          | `--engine agy`                                                           |
| Claude (Opus, Sonnet, Haiku, Fable)                             | `--engine claude` (Claude Code; `--claude-config-dir` picks the profile) |
| GLM, DeepSeek, Qwen, Kimi, MiniMax and other open-weight models | `--engine opencode` (OpenRouter)                                         |

The runner enforces this: `--engine opencode` with a Claude, GPT or Gemini model exits 17 and names
the native engine. Never work around it with a different provider prefix. Antigravity's own Claude
and GPT-OSS pool (`agy` models) is a native Antigravity route and stays allowed.

## Choose ownership before a model

| Situation                                                                        | Action                                                                              |
| -------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Decisions, failed approaches or coupled changes live mainly in this conversation | Continue here                                                                       |
| Another agent can finish from a portable packet and you can assess the outcome   | Delegate, if it beats the briefing and review cost                                  |
| A finished worker leaves a small fix you already understand and can verify       | Finish it here; another round trip usually costs more than the fix                  |
| The owner lacks one fact, diagnosis or perspective                               | Consult narrowly and keep ownership                                                 |
| The problem needs exploration before a solution or hypothesis is known           | Dispatch `--kind investigation`; let the investigator choose its read-only approach |

Delegate substantial, self-contained work so you can advance other authorized parts while it runs.
Prepare integration, check evidence, or finish independent work instead of waiting when useful work
is available. Wait only when the next useful action depends on the worker, and keep its leased
worktree frozen.

Before implementation delegation, state the objective, exact scope, acceptance criteria, versions
and conventions, decisions to preserve, failed attempts and evidence pointers (the task-spec
`context_packet` holds the last four). For consultation, state the decision needed and what evidence
would change it. Never send secrets, credentials or unrelated private files; the brief goes to the
model provider. Read [references/coordinator.md](references/coordinator.md) for consultation,
correction, Pointdexter evidence checks, or recovery across turns.

## Pick the route

An explicit user request beats every default. Otherwise use a role from `routes`:

```bash
python3 <skill-directory>/scripts/run_agent.py routes
```

| Role                           | Default                             |
| ------------------------------ | ----------------------------------- |
| `implementation` (the default) | `codex gpt:luna --effort max`       |
| `consultation` / audit         | `codex gpt:sol --effort high`       |
| `gemini`                       | `agy gemini:fast`                   |
| `claude`                       | `claude claude:best --effort high`  |
| `opencode`                     | `opencode glm:flash --variant high` |

Models are family selectors (`family` or `family:tier`, optional `latest:` prefix) that resolve to the
newest live model, so a release needs no edit here. GPT and Gemini pick the highest version number in the
live catalog; Claude Code's aliases and OpenRouter's `~vendor/...-latest` aliases are provider-maintained.
`--model deepseek` alone infers the engine. `run_agent.py families` shows what every selector resolves to;
`references/families.json` holds the rules, and a `families` key in your config adds or overrides one. An
exact ID pins a model, and the runner prints `AGENT_NOTE=model_advanced` on stderr when a selector moves.

Pass `--route <role>`; explicit `--engine/--model/--effort` still win. The user's
`~/.config/agent-executor/config.json` overrides these (`init` writes a starter). Never invent a model
ID: check with a filtered query such as `models --engine codex --match sol`. If a preferred model is
missing, report it and ask rather than silently substituting.

## Check quota

```bash
python3 <skill-directory>/scripts/run_agent.py quota
```

It reads live [ai-usage](https://github.com/loftshed/claude-whisperer/tree/main/plugins/ai-usage) data.
The route table decides which models fit the task; quota only chooses among fitting routes and never
justifies a weaker model. Prefer pools marked expiring (capacity lost at the weekly rollover), then the
most unused capacity. The runner refuses a blocked pool (exit 15; `--ignore-quota` only on the user's
say-so). A pool on `credits` (Codex once its weekly allowance is used up, with a credit balance) is not
blocked: the run goes ahead, metered against the balance, and prints `AGENT_LIFECYCLE=quota_credits`
with the balance. When the user asked for that route, launch it and note the credits in one line of
the report ("on credits, 62,168 cr left"), nothing more; only choose it yourself when no fitting route
has free quota, since it ranks after pools with free quota. Never switch billing context (work vs
personal account) on quota grounds without the user. Without ai-usage, quota is unknown and runs
proceed.
The `opencode` row is the OpenRouter account's remaining dollar credit (key from `OPENROUTER_API_KEY` or
OpenCode's `auth.json`; the sandbox needs `openrouter.ai` allowed). It is informational: the launch gate
never blocks OpenCode.

## Safeguards

1. Scope: a JSON `--task-spec` carries authoritative `allow_paths`/`track_paths`; a handwritten brief
   uses repeatable `--allow-path` and `--track-path`. Mutating runs add `--expect-changes`.
2. The executor must not commit, push, merge, reset, clean, stash, checkout, switch branches, create
   worktrees, publish, or touch secrets. A change to this worktree's branch, HEAD, reflog or stash fails
   the run.
3. The report has five headings in order: STATUS, FILES CHANGED, COMMANDS RUN, VERIFICATION, RISKS OR
   BLOCKERS. STATUS is `COMPLETE`, `BLOCKED` or `FAILED`.
4. Deterministic gates go in repeatable `--verify-command`; they run outside the model after a clean
   handoff and must not mutate.
5. While jobs run, freeze their worktree against edits. Empty-scope readers may run together;
   a writer holds an exclusive lease. The conductor supervises every active run.

## Run

1. Record `git status --short`, branch and `HEAD`. Plan completely.
2. Write a task spec per [references/brief-template.md](references/brief-template.md), outside the repo.
3. Launch:

   ```bash
   python3 <skill-directory>/scripts/run_agent.py \
     --cwd <absolute-repository-root> --task-spec <spec.json> --route implementation \
     --verify-command 'python3 -m unittest tests.test_jobs' --expect-changes
   ```

   Run it in the foreground through the host's background-task primitive. Where the host cannot
   supervise, add `--detach` and use `events wait <AGENT_EVENT> --messages --timeout 30m`.
   Answer or acknowledge each worker message, then wait again until completion. Never poll for status.
   Before a new run in a repository, read its inbox once (`events --cwd <repo>`). Details:
   [references/completion.md](references/completion.md).

4. While supervising, handle live questions and updates through the run's `AGENT_CHANNEL`.
   For foreground runs without a completion event, use `messages wait CHANNEL --recipient conductor`.
   See [references/communication.md](references/communication.md). Once complete, review `result.json`,
   starting with `review`: `status`, `task_outcome`, `verification`, `run_delta`,
   `scope_violations`, `history_violations`, `provider_error`. Then read the full diff and map every
   acceptance criterion to code, a test or an observation. A passed runner gate need not be rerun.
5. If review finds a gap, prefer finishing a small, understood fix yourself after the worker has
   stopped. Use the guidance below to choose local completion or one focused worker correction.
   Never repeat an unchanged failed prompt.
6. Acknowledge the event (`events --ack <id>`) only after review.

## Finish small gaps locally

Default to conductor completion when the remaining fix is understood, within the authorized scope,
and faster to make and verify than to brief, wait for, and review another worker turn. Examples
include formatting, a missing assertion with a known expected result, or a small source change whose
behavior is already established. A worker getting most of the task done is useful; it does not need
to make every final edit.

Send one focused correction only when substantial work or investigation remains and the worker's
context makes it better placed to finish. Resume the same session and route with `--session <id>
--resume-result <result.json>`. Do not send work back merely to obtain a `COMPLETE` report or because
a correction invocation is available. If the approach is wrong or repeated corrections would cost
more than taking ownership, take the work back.

Wait for terminal completion and release of the worktree lease before editing. Record the review
and conductor takeover first when using the coordinator. Preserve the original worker outcome,
identify the conductor's edits, and run checks affected by those edits. "Almost done" never lowers
the acceptance criteria. Scope or history violations require rejection and do not permit resuming
that run; inspect any proposed changes independently before deciding what can be retained.

## Steer a running job

For ordinary questions, answers, and discoveries, use the live message channel. It preserves the
running executor and consumes no additional executor invocation. Steering is for an urgent change
that cannot wait for the worker's next communication checkpoint.

When you learn something mid-run that should change a detached job's direction (a new root-cause
lead, a wrong assumption), steer it instead of killing and relaunching, which loses its context:

```bash
python3 <skill-directory>/scripts/run_agent.py steer <AGENT_EVENT_ID> --cwd <repo> --message @lead.md
```

The job's runner interrupts the executor (SIGINT, then SIGTERM), resumes the same session in the same
worktree with your message as a new turn inside the original report contract, and keeps the lease,
scope, gates, timeouts and notification throughout. It prints a new `AGENT_EVENT_ID`; an `events wait`
on the old ID follows the job there. Scope, history and gates are judged over the combined run from the
original snapshot, and the message is kept under `<job>/steers/<n>/`. Write the message like a
correction: evidence and the changed direction, never new scope. A finished job is refused (exit 30):
correct it with `--session <id> --resume-result <result.json>`. Details: [references/runner.md](references/runner.md#steering-a-running-job).

## Status and exit codes

| Exit              | Status                                          | Meaning / action                                                                                                                                              |
| ----------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0                 | `completed`                                     | Clean run, gates passed. Still review the diff                                                                                                                |
| 1                 | `failed`                                        | Executor failed or reported FAILED                                                                                                                            |
| 3                 | `empty_output`                                  | No output; check `provider_error` and logs                                                                                                                    |
| 4                 | —                                               | Bad arguments, spec, route or effort; fix the call                                                                                                            |
| 11 / 12 / 13      | — / `timed_out`                                 | Catalog failure / timeout / CLI not installed                                                                                                                 |
| 15                | —                                               | Quota pool exhausted at launch and no credit balance; pick another fitting route or wait for refill                                                           |
| 16                | `provider_quota_exhausted`                      | The provider's own error says quota, credits or rate limit (e.g. 429); retry later or elsewhere                                                               |
| 17                | —                                               | Hosted model sent to OpenCode; use the native engine named in the error                                                                                       |
| 20                | `no_changes`                                    | `--expect-changes` but nothing changed                                                                                                                        |
| 21                | `history_violation`                             | This worktree's branch/HEAD/reflog/stash changed                                                                                                              |
| 22                | `scope_violation`                               | Changes outside the allowed paths                                                                                                                             |
| 23                | `malformed_report`                              | Report headings or STATUS unusable                                                                                                                            |
| 24                | `blocked`                                       | Executor reported a blocker; consult or resume with evidence                                                                                                  |
| 25 / 26           | `verification_failed` / `verification_mutation` | A gate failed / a gate changed files                                                                                                                          |
| 27                | —                                               | Another runner holds the worktree lease                                                                                                                       |
| 28                | —                                               | Task deadline passed before dispatch                                                                                                                          |
| 30 / 31 / 32 / 33 | —                                               | `steer`: job already finished / job in another worktree / runner process not found / steer rejected (no session ID yet, not taken in time, or a pre-0.11 job) |

Never present delegation as successful unless the status is `completed` and your review accepts the diff.
[references/runner.md](references/runner.md) covers engines, catalogs, effort, usage and cost,
audit rules and retention (`prune`).

## Completion report

Deliver it exactly once. It ends the task: no acknowledgments, waves or repeated summaries afterward.

```text
Agent executor: <status>
Task outcome: complete | blocked | failed
Engine / model / effort or variant: <exact values>
Run artifacts: <result.json path>
Changes: <concise file summary>
Runner-owned verification: <commands and outcomes>
Remaining risks: <none or concise list>
```

## Skill development gate

```bash
# from the repository root; tests live outside the shipped skill
python3 -m py_compile plugins/agent-executor/skills/agent-executor/scripts/*.py
python3 -m unittest discover -s tests/plugins/agent-executor/skills/agent-executor -p 'test_*.py'
```
