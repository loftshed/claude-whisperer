# bailout

Writes a handoff before the five-hour Claude allowance runs out, so the next
agent can continue without the task being explained again. The next agent can be
Codex, Gemini, or another Claude session; nothing here depends on which.

The package detects when to bail out, asks for the handoff, and yields. It does
not launch, route to, or coordinate with any other agent.

## How it works

| Piece | Mechanism | Runs |
| --- | --- | --- |
| Sampler | Appended to the status line script | Every assistant message |
| Gate | `PostToolUse` hook | After each tool call |
| Yield | `Stop` hook | End of turn, only when a bailout is outstanding |
| Recovery | `SessionEnd` hook | Session ends with the handoff still missing |

The status line is the only supported local source of the real subscription
usage figure: `rate_limits.five_hour.used_percentage` is documented as a status
line field, and hook payloads do not carry it. So the sampler reads it there and
writes it to a state file, and the `PostToolUse` hook is what actually reaches
Claude mid-task, without waiting for a new prompt.

Nothing calls a model to find out about usage. The sampler is a JSON read.

### Sequence

1. **90% consumed** — one short checkpoint is requested, then ordinary work
   continues. Once per quota window. Disable with `"checkpointEnabled": false`.
2. **95% consumed** — the handoff is requested, ordinary work stops, and the
   turn ends with the absolute path reported.
3. **Handoff missing at the end of the turn** — the `Stop` hook asks again, at
   most `maxStopBlocks` times, then lets the turn end regardless.
4. **Session ends anyway** — a clearly labelled recovery snapshot is assembled
   from the last checkpoint plus git state. No model request is made, and it
   does not pretend to contain reasoning Claude never wrote.

Neither threshold guarantees capacity for another response. One large operation,
or other activity on the account, can spend the remainder first. This is built
for preservation, not for a guaranteed quota ceiling.

### Model-aware thresholds

Heavier models consume the allowance faster, so a single sample can jump further
before the next one lands. Their thresholds are pulled earlier by a configurable
margin: 3 points for Opus and Fable, 1 for Sonnet, 0 for Haiku. On Opus the
bailout therefore fires at 92%, not 95%. Keys match as substrings of the model id.

## Install

```bash
node ~/.claude/skills/bailout/scripts/install.mjs --dry-run   # see what it would do
node ~/.claude/skills/bailout/scripts/install.mjs
```

It merges three hook entries into `~/.claude/settings.json` and appends a marked
block to whatever status line script is already configured. It backs up both
files first, changes nothing else, and adds no permissions. Running it twice
changes nothing the second time.

If no status line is configured, it writes one to `~/.claude/bailout/statusline.sh`
that samples usage and prints `Opus · 41% of 5h`. If a status line is configured
but does not read stdin into `$input`, the installer refuses to guess and prints
the line to add by hand.

Restart Claude Code afterwards so it picks up the new hooks.

The interpreter baked into the hooks is a stable alias such as
`/opt/homebrew/bin/node`, never a version-pinned path like
`/opt/homebrew/Cellar/node/26.9.0/bin/node`, which would vanish at the next node
upgrade. The sampler discards its own stderr, so that failure mode is silent:
`bailout.mjs status` checks for it and reports `install: PROBLEM` with the
missing path, and rerunning the installer repairs the block in place.

### Uninstall

```bash
node ~/.claude/skills/bailout/scripts/install.mjs --uninstall
```

Removes only its own hook entries and its own status line block. A status line
you changed after installing is left alone. Delete `~/.claude/skills/bailout/`
and `~/.claude/bailout/` to remove the rest.

## Use

- `/bailout` — write the final handoff now, at any time, with no usage data needed.
- `/bailout checkpoint` — write a short checkpoint and keep working.
- Automatically, when a threshold is crossed.

```bash
node ~/.claude/skills/bailout/scripts/bailout.mjs status   # reading, thresholds, stage
node ~/.claude/skills/bailout/scripts/bailout.mjs paths    # where the files go
node ~/.claude/skills/bailout/scripts/bailout.mjs recover  # recovery snapshot now
node ~/.claude/skills/bailout/scripts/bailout.mjs reset    # rearm this session
```

Handoffs are written to
`~/.claude/bailout/handoffs/<project>/<session>/{checkpoint,handoff,recovery}.md`,
one directory per project and session, outside any tracked source tree. Nothing
is uploaded anywhere.

## Configuration

Copy `config.example.json` to `~/.claude/bailout/config.json` and edit. Any
subset works; unset keys keep their defaults. `BAILOUT_HOME` relocates the whole
state directory, which is how the tests avoid touching real state.

## What is verified, and how

Run the suite with `node ~/.claude/skills/bailout/tests/run-tests.mjs`. 37 tests,
no dependencies, each in its own temporary directory, none of it spending any
real allowance.

Covered by fixtures: threshold crossings, starting already above 95%, model
margins, configurable and disabled thresholds, missing data, non-numeric data,
malformed stdin, a corrupt state file, stale samples, quota window rollover,
a handoff left over from the previous window, repeated events, separate sessions
and projects, subagents, manual invocation with no telemetry, bounded stop
blocks, a failed replacement write leaving the previous checkpoint intact,
recovery with and without a checkpoint and with and without git, and install,
reinstall, dry run and uninstall against a settings file and status line script
that already had their own contents.

Verified against real Claude Code, not only fixtures:

- `PostToolUse` fires mid-task and its `additionalContext` reaches Claude, which
  quoted the injected text back verbatim.
- The `Stop` hook's `decision: "block"` is honoured: with a bailout armed, the
  turn was held, Claude wrote the handoff, and the next stop was allowed
  (`stopBlocks: 1`, `stage: bailout-done`).
- Hooks passed through `--settings` fire in print mode, and environment
  variables reach them.
- This machine's status line payload really does carry
  `rate_limits.five_hour.used_percentage`.

## Limits

- **Interactive only for the automatic trigger.** Status lines do not run under
  `claude -p`; this was tested, not assumed. Hooks do fire headless, so a
  headless session yields on a bailout armed elsewhere, but nothing samples
  usage there. `/bailout` works in every mode.
- **`rate_limits` needs a Pro or Max subscription** and appears only after the
  session's first API response. Missing data reads as unknown, never as zero,
  and never as context-window fullness, elapsed time, or a token estimate.
- **Claude Code drops a rate-limit window once it resets**, so there is a gap
  with no reading just after a reset.
- The sampler depends on the configured status line script continuing to call
  it. A tool that rewrites that script can drop the block; `bailout.mjs status`
  reporting `unknown` on a healthy session is the symptom.
- The sampler costs about 66 ms per assistant message, nearly all of it Node
  startup. Claude Code debounces status line updates at 300 ms and cancels an
  in-flight script when a new update arrives, so individual samples can be
  dropped during rapid activity. The thresholds are coarse enough that this
  does not matter, and state is written atomically, so a cancelled sampler
  cannot leave a partial file.
- Context-window pressure and other providers' quotas are out of scope.

## Attribution

Written from scratch. `arshitP/claude-usage-limit-handoff` (MIT) and
`MG-Cafe/claude-budget-rescue` (no licence, so nothing could be reused from it)
were read for ideas only; no code was taken from either, and neither one's
automatic-continuation behaviour was carried over.
