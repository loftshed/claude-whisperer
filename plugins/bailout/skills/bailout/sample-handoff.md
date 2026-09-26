# Sample handoff

A real handoff, written for the session that built this skill, captured just
before the installer was run. It is the length and shape the skill aims for.

---

> Continue the task described below. First check the current files and any running
> work against this handoff, then take the stated next action. Preserve the user's
> constraints and existing work. Treat unfinished or unverified items as such.

**Kind:** final bailout
**Written:** 2026-09-18T12:40:00Z
**Working directory:** `~/Documents/example-project`
**Session:** 9fbc9ba5
**Transcript (supplementary, Claude's own format):** `~/.claude/projects/-Users-me-Documents-example-project/e941a233…jsonl`

## What the user wants

Build a standalone Claude Code skill that writes a handoff before the five-hour
usage allowance runs out. His words: *"This is just a bailout thing whenever
you're going to hit the end of a session, so I don't have to figure out how to
describe what was going on to the next agent that's going to take over when my
usage runs out."*

The spec is `/Users/lofstpe1/Downloads/claude-bailout-standalone-build-brief.md`.
Two constraints it states repeatedly, both load-bearing:

- **Strictly standalone.** It must not integrate with, modify, or even inspect
  his `agent-executor` skill or any orchestrator, and must not route to, launch,
  or select another agent. Its job ends when the handoff is saved and the agent
  yields. An earlier brief asked for the opposite; that one is withdrawn.
- **Do not claim it is automatic unless automatic triggering has been verified.**
  Missing telemetry is "unknown", never zero, and never silently substituted with
  context-window fullness, elapsed time, or a dollar budget.

Mid-task he added one requirement: thresholds should be **model-aware**, because
some models consume the allowance much faster, so on a heavy model the handoff
should be written "a few percentage points further away from the end".

Done means: skill, scripts, tests, install and removal instructions, configurable
thresholds, a sample handoff, and an explicit statement of what is installed
versus merely prepared and what was tested with fixtures versus live.

## Where we stopped

The package is written and its tests pass. It is **not yet installed** — the
hooks are not in `~/.claude/settings.json` and the sampler is not yet in the
status line, so nothing triggers automatically on this machine yet.

Everything lives in `/Users/lofstpe1/.claude/skills/bailout/`.

## What matters from the conversation

- **The status line is the only supported source of the real usage figure.**
  `rate_limits.five_hour.used_percentage` is a documented status line field;
  hook payloads do not carry it (checked against the hooks reference). That is
  why there is a sampler in the status line at all, rather than everything living
  in one hook.
- **His status line is owned by Vibe Island** (`~/.vibe-island/bin/vibe-island-statusline`)
  and every hook event already has a vibe-island bridge registered. The installer
  therefore merges and appends; it must never replace. Its own file says to add to
  it rather than change `statusLine.command`, and it preserves added content.
- **Automatic triggering is interactive-only.** Status lines do not run under
  `claude -p`; this was tested rather than assumed. Do not describe the headless
  path as automatic.
- Rejected: putting this in his `claude-plugin-marketplace` repo. That repo
  distributes plugins to his team, and the brief calls for something standalone
  and personal.
- Rejected: a machine-readable task manifest or task database. The brief rules it
  out; a Markdown handoff plus minimal state is the whole design.
- Two real bugs were found by the tests and fixed: a generated status line was
  being written into the skill folder where a skill update would clobber it, and
  a `handoffs` map that stayed sticky let a handoff from a previous quota window
  satisfy a fresh bailout. The fix was per-kind arm timestamps in `state.armed`.

## Files and environment

All new, all under `/Users/lofstpe1/.claude/skills/bailout/`:

- `SKILL.md` — the `/bailout` skill: procedure and the six required sections.
- `scripts/lib.mjs` — config, thresholds, atomic writes, state paths.
- `scripts/hooks.mjs` — four modes: `statusline`, `gate`, `stop`, `session-end`.
- `scripts/recovery.mjs` — mechanical snapshot, no model request.
- `scripts/bailout.mjs` — `paths`, `status`, `recover`, `reset`, `simulate`.
- `scripts/install.mjs` — merge-only installer and uninstaller.
- `tests/run-tests.mjs` — 37 tests, no dependencies.
- `README.md`, `config.example.json`, `sample-handoff.md`.

Nothing in the `example-project` working tree was touched; its dirty files
are the user's own in-flight table work and are unrelated. No commits were made.

Note: `~/.claude/skills` is write-denied under the Bash sandbox on this machine,
so writes there need `dangerouslyDisableSandbox`. Reads and test runs are fine
sandboxed.

## Verification

- `node ~/.claude/skills/bailout/tests/run-tests.mjs` → **37 passed, 0 failed**.
- Live against real Claude Code (`claude -p`, Haiku, scratch settings): the
  `PostToolUse` gate fired mid-task and Claude quoted the injected text back
  verbatim; with a bailout armed, the `Stop` hook held the turn, Claude wrote the
  handoff, and the next stop was allowed (`stopBlocks: 1`, `stage: bailout-done`).
- Confirmed by experiment that the status line does **not** run under `claude -p`.
- **Not verified:** the installer has not been run against the real
  `~/.claude/settings.json` or the real Vibe Island status line, and no automatic
  trigger has yet fired from genuine quota pressure in an interactive session.

## Do this next

Run the installer against the real configuration and confirm it merged cleanly:

```bash
node ~/.claude/skills/bailout/scripts/install.mjs --dry-run
node ~/.claude/skills/bailout/scripts/install.mjs
```

Then check that the three vibe-island hook groups still exist alongside the new
ones in `~/.claude/settings.json`, that `statusLine.command` is unchanged, and
that the status line still renders. After restarting Claude Code, confirm
`node ~/.claude/skills/bailout/scripts/bailout.mjs status` shows a real
percentage rather than `unknown` — that is the proof the sampler is wired in.
