# Consultation: `run_agent.py consult`

One focused question to a more capable model, one answer back. The caller states its intent,
supplies the question, enough context to answer it, and optionally a model preference; the runner
resolves the model and the transport, and returns the answer, the model that answered, and the
material uncertainty the consultant stated. A consultation grants no permission to edit files,
publish, or take actions: the consultant answers under the engine's no-write mode, and its answer
authorizes nothing.

Implementation delegation is a different workflow with scope, worktree leases, audit and
runner-owned verification; see [SKILL.md](../SKILL.md). Do not turn a question into a task spec to
ask it, and do not turn an answer into an authorization to act. Asking a live session of another
harness, with its own context, is the peer mailbox, not a consultation.

## Intents

| Intent               | Need                         | The consultant gets                                        | Reply headings                        |
| -------------------- | ---------------------------- | ---------------------------------------------------------- | ------------------------------------- |
| `question` (default) | "Am I missing something?"    | The prompt only, in an empty workspace                     | `ANSWER`, `UNCERTAINTY`               |
| `investigate`        | "Find out why this happens." | Read access to one repository (`--read`)                   | `FINDINGS`, `EVIDENCE`, `UNCERTAINTY` |
| `review`             | "Check my work."             | Read access plus the captured diff (`--read --diff [REF]`) | `FINDINGS`, `EVIDENCE`, `UNCERTAINTY` |

`--read` alone implies `investigate`. A review's findings carry severity, file and line, and a
concrete fix, most severe first; an investigation's findings put the most likely cause first with
its confidence. Both quote their evidence and say what they did not check.

```bash
python3 <skill-directory>/scripts/run_agent.py consult \
  --question 'Is unlinking a flock file after close safe on macOS?' \
  --context 'We hold the lock across fork; see the attached module.' \
  --attach src/lock.py --model gpt:sol --effort high

python3 <skill-directory>/scripts/run_agent.py consult --intent review \
  --read . --diff origin/main --question 'Anything unsafe in this change?' --model claude:best
```

| Input        | Form                                                                                                                            |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| `--question` | Text, `@FILE`, or `-` for stdin. Required.                                                                                      |
| `--context`  | Text or `@FILE`; repeatable, joined in order.                                                                                   |
| `--attach`   | A text file inlined under its name (relative path inside `--read`); 128 KiB each, 200 KiB prompt.                               |
| `--read`     | A Git repository (any directory inside it) the consultant may read and never write.                                             |
| `--diff`     | `git status --short` and `git diff REF` (default `HEAD`) from `--read`, inlined; 128 KiB at most.                               |
| `--model`    | Exact ID, family selector (`gpt:sol`, `claude:best`, `deepseek`) or Claude alias (`opus`).                                      |
| `--engine`   | Only when the model does not say: GPT to Codex, Claude to Claude Code, Gemini to Antigravity, open-weight families to OpenCode. |
| `--effort`   | Codex catalog effort, Claude `low…max`; Antigravity takes it from the model slug.                                               |
| `--route`    | Role from `routes` (default `consultation`: `codex gpt:sol --effort high`).                                                     |
| `--timeout`  | Default 10m for a question, 20m with `--read`.                                                                                  |

Attachments are refused when their name or directory says credentials, on the path as given and
on what it resolves to (`.env*`, `*.pem`, `*.key`, `id_rsa*`, anything with `secret` or `credential`
in the name, `.netrc`, `.npmrc`, `.pypirc`, or anything under `.ssh`, `.aws`, `.gnupg`, `.kube`,
`.docker`), and when they are binary. Quote redacted text through `--context` instead; the prompt
goes to the model provider. Host paths of attachments stay out of the prompt. A `--read` repository
is different: the consultant can read whatever is in it, untracked files included, so do not point
it at a checkout holding secrets.

## Model and transport

The model is resolved exactly as for a run: an explicit ID is checked against the live catalog, a
selector resolves to the newest matching live ID, and a Claude alias is passed through (an exact
`claude-*` ID is accepted unverified). A hosted family sent to OpenCode exits 17. An explicit choice
is never substituted; when the requested model is unavailable the command fails with the closest
live IDs rather than answering on another model. The quota gate applies (exit 15 on an exhausted
pool, `--ignore-quota` only on the user's say-so), and a `--claude-config-dir` names the Claude
profile that pays.

The host is read from the environment (`CLAUDECODE`, `CODEX_*`, `ANTIGRAVITY_*`, `OPENCODE*`),
or given with `--host`. `--transport auto` (the default) then decides:

| Situation                                                          | Transport        |
| ------------------------------------------------------------------ | ---------------- |
| The calling harness runs this model family itself                  | `native_handoff` |
| … but `--claude-config-dir` chose another Claude profile (billing) | executor         |
| Any other engine, `--transport executor`, or a `--follow-up`       | executor         |
| `--transport native` from a host that does not run the model       | refused (exit 4) |

A native handoff writes the rendered prompt to `prompt.md`, records a `native_handoff` result, and
prints how to ask through the host's own agent tools (Claude Code's Agent tool, a Codex sub-agent
thread, Antigravity's `invoke_subagent`, OpenCode's plan agent). The caller runs that one native call,
read-only, and reports the sections, the model the native tool used, and the uncertainty. It never
starts a CLI copy of its own harness to avoid the handoff. A native consultation has no runner
session, so its follow-ups go to the same native agent; only executor consultations take
`--follow-up`.

## What the consultant can do

| Engine     | Question (empty workspace)                                                                              | With `--read` (in the repository)                                              |
| ---------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `codex`    | `sandbox_mode="read-only"`, `approval_policy="never"`: no writes; read tools and read-only shell remain | the same, in the repository                                                    |
| `claude`   | `--tools ""` and `--strict-mcp-config`: no tools at all                                                 | `--tools Read,Grep,Glob` and `--strict-mcp-config`: read tools only            |
| `agy`      | `--mode plan`, no `--dangerously-skip-permissions`: no writes; read tools remain                        | the same, with the repository added                                            |
| `opencode` | `run --standalone` with every tool and MCP server disabled: no tools at all                             | `run --standalone` with only `read`, `glob` and `grep` enabled, no MCP servers |

Claude Code and OpenCode can be held to no tools. Codex and Antigravity run in their no-write
modes: they keep read tools, and Codex keeps a read-only shell. Antigravity's headless plan mode
denies a command and then ends the turn with no answer (status `empty_output`, with the denied
action in `denied_actions`), so the prompt tells every engine but Codex that it cannot run
commands; a `--follow-up` on that empty result nudges the same session to answer from what it
read. OpenCode needs its private server (`--standalone`): the shared background service keeps the
configuration it started with and would hand the consultant the user's full tool set. The host
session's Claude tokens and profile are scrubbed from every consultant. The result records the
policy under `tool_policy` and `writes_allowed: false`.

These boundaries were checked live on each engine with a consultant asked to create a file: Codex's
sandbox refused the write, Claude Code and OpenCode had no tool that could write, and Antigravity
denied the command.

With `--read`, the consultant shares a reader lease on the repository (an implementation run
holding it exclusively refuses the consultation with exit 27), and the runner fingerprints the
repository before and after. A difference is reported as `repository_delta` and an
`AGENT_NOTE=repository_changed_during_consultation` line, not judged: the engine's no-write mode is
what prevents edits, and the user or the conductor may legitimately edit while a reader looks.
Treat a non-empty delta as something to explain.

## Result

`result.json` (`agent-executor.consultation.v1`) in `<cache>/consults-v1/<stamp>/` (or
`--out-dir`), and on stdout:

```text
AGENT_CONSULT_STATUS=answered
AGENT_INTENT=question
AGENT_TRANSPORT=executor
AGENT_ENGINE=codex
AGENT_MODEL=gpt-5.6-sol
AGENT_MODEL_OBSERVED=<only when the provider reported it>
AGENT_SESSION=<session id>
AGENT_RESULT=<result.json>
--- ANSWER ---
…
--- UNCERTAINTY ---
…
--- END ---
```

`--format json` prints the result instead. Fields worth reading: `sections` (by heading), `answer`
(the main section: ANSWER or FINDINGS), `uncertainty`, `answer_format` (`structured` when every
expected heading is present, `partial` when some are, `unstructured` when the consultant ignored
them and the whole reply is the answer; headings quoted inside code fences do not count), `model`
(dispatched, with `model_preflight` saying how it was checked), `model_observed` (IDs the provider
itself reported: Antigravity's `init.model`, Claude Code's `modelUsage`; empty when the CLI exposes
none), `effort`, `claude_config_dir`, `session_id`, `repository_delta`, `usage` and
`tariff_estimate` (reference estimates, not an invoice), `final_message` (the bounded raw reply).

Report the answer as the consultant's, with its uncertainty, and name the model from `model`
(and `model_observed` when present). The answer is advice: it does not approve anything, widen
scope, or verify evidence.

| Exit | Status                     | Meaning                                                         |
| ---- | -------------------------- | --------------------------------------------------------------- |
| 0    | `answered`                 | One answer; read `uncertainty`                                  |
| 0    | `native_handoff`           | Ask through the host's native tools with `prompt_path`          |
| 0    | `dry_run`                  | Inputs and command resolved; no model called                    |
| 1    | `failed`                   | The CLI or provider failed; see `error`, `stderr_tail`          |
| 3    | `empty_output`             | The consultant returned nothing                                 |
| 4    | —                          | Bad arguments, refused attachment, impossible transport         |
| 12   | `timed_out`                | No answer within `--timeout`                                    |
| 15   | —                          | Quota pool exhausted at launch                                  |
| 16   | `provider_quota_exhausted` | The provider said quota, credits or rate limit; retry elsewhere |
| 17   | —                          | Hosted model sent to OpenCode; use its native engine            |
| 27   | —                          | An implementation run holds the `--read` repository             |

## Follow-up questions

```bash
python3 <skill-directory>/scripts/run_agent.py consult \
  --question 'And if the descriptor is inherited across exec?' --follow-up <result.json>
```

The follow-up resumes the session that answered (Codex `exec resume`, Claude Code `--resume`,
Antigravity `--conversation`, OpenCode `--session`) in the directory it started in, on the same
model, effort, variant and Claude profile, with the same intent and repository. Asking to change
any of these is refused, because a different model, intent or repository is a fresh consultation.
New context, attachments and a fresh `--diff` may be added. Usage is reported since the previous
result's baseline where the engine's counters allow it.

## Asynchronous consultations

```bash
python3 <skill-directory>/scripts/run_agent.py consult --detach --question @q.md --attach notes.md
python3 <skill-directory>/scripts/run_agent.py events wait <AGENT_EVENT_ID> --timeout 15m
```

`--detach` resolves the model, effort, route rule and quota first and pins the resolved model, then
answers in a background job under `consults-v1/` and prints `AGENT_JOB`, `AGENT_RESULT` and
`AGENT_EVENT`. Wait once with `events wait`; the event carries `status` and `result_path`. A failure
before the answer still publishes the event. Acknowledge it (`events --ack <id>`) after reading the
result. `--notify desktop` (the default for detached consultations) sends the usual desktop
notification. A native handoff cannot detach: the host's own tools run it asynchronously.

## Troubleshooting

- `cannot tell which engine runs '<model>'`: the model is neither a family selector nor a hosted
  family name. Pass `--engine`.
- `native_handoff` when you wanted the CLI: pass `--transport executor` (another profile,
  `--claude-config-dir`, or a deliberately separate process).
- `answer_format: unstructured`: the consultant ignored the headings; the whole reply is in
  `answer` and `uncertainty` is `null`. Ask a follow-up for the uncertainty if it matters.
- The consultant says it needs to read something: for one file, `--attach` it; for a codebase,
  ask again with `--read`. Never grant it write access; it has none on purpose.
- `repository_delta` is not empty after a `--read`: find out who changed the repository while the
  consultant read it before trusting evidence that cites those paths.
- `prune` removes consultation directories older than the cutoff like runs and jobs; an
  unacknowledged detached consultation is kept.
