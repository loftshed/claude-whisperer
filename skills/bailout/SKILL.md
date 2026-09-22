---
name: bailout
description: Write a handoff file that lets another agent continue this task without the user re-explaining it. Use when the five-hour Claude usage allowance is nearly spent, when a bailout or checkpoint threshold notice appears in context, or when the user asks to bail out, hand off, or save context before running out of usage.
argument-hint: "[checkpoint|final]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/bailout.mjs *), Bash(node ${CLAUDE_SKILL_DIR}/scripts/bailout.mjs *)
---

# Bailout handoff

Preserve enough of the current task that a fresh agent can continue it. The reader
has none of this conversation and may be Codex, Gemini, or a later Claude session.
Nothing here depends on which one.

## Procedure

1. Get the paths for this session:

   ```bash
   node ${CLAUDE_SKILL_DIR}/scripts/bailout.mjs paths ${CLAUDE_SESSION_ID}
   ```

   This works whether or not usage telemetry is available.

2. Write the file, following the sections below.
   - `checkpoint` argument, or a checkpoint threshold notice: write `checkpoint.md`,
     keep it to a few hundred words, then carry on with ordinary work.
   - `final`, no argument, or a bailout threshold notice: write `handoff.md`.
     Start no new work after it. If `checkpoint.md` exists, build on it rather
     than starting over, and correct anything in it that has since changed.

3. Report the absolute path, then give the user this line verbatim, with the path
   filled in:

   > Read `<absolute path>` and continue from the next action.

   Do not claim another agent has started.

## Required sections

Lead with this instruction, unedited:

> Continue the task described below. First check the current files and any running
> work against this handoff, then take the stated next action. Preserve the user's
> constraints and existing work. Treat unfinished or unverified items as such.

Then a short header: timestamp, working directory, session id, and whether this is
a **checkpoint**, a **final bailout**, or an **emergency recovery**.

Then these six sections.

1. **What the user wants.** The real objective, the constraints, the latest
   corrections, and what "done" means. Quote the user exactly wherever a paraphrase
   would lose a distinction. Corrections made mid-task matter more than the opening
   request.
2. **Where we stopped.** Done, in progress, and the exact interruption point.
3. **What matters from the conversation.** Decisions and why, approaches tried and
   rejected and why, open questions, and preferences the next agent would otherwise
   trip over. Rationale, not a transcript, and not a reasoning trace.
4. **Files and environment.** Absolute paths, each changed file and what it is for,
   branch and commit if any, prerequisites, and any pending command or background
   job. Do not claim edits that were already there before this session.
5. **Verification.** Commands actually run and what they actually printed. Failures
   included. What still needs checking. An earlier pass is not current evidence,
   and an assumption is not a result.
6. **Do this next.** One concrete first action, then a short continuation plan.

## Rules

- Prioritise what cannot be recovered by reading the files. Reasons, rejected
  options and the user's exact constraints are the valuable part; a list of
  function names is not.
- A few hundred words to about 1,000. Go longer only when the context genuinely
  requires it.
- No secrets, credentials, tokens, environment files, or unrelated personal
  content. Refer to a config file by path rather than pasting it.
- Never clean, reset, stash, commit, or reorganise the user's work to make the
  handoff tidier. Describe the working tree as it is.
- Mention the transcript path as a supplement if one is known, but the handoff has
  to stand on its own without it.
- Keep it local. Do not upload it anywhere.

## Thresholds

Automatic triggering samples the five-hour subscription allowance from the status
line and fires once per quota window: a checkpoint at 90% consumed, the bailout at
95%. Heavier models reach the ceiling in fewer turns, so their thresholds are
pulled earlier — 3 points for Opus and Fable, 1 for Sonnet, 0 for Haiku. Edit
`~/.claude/bailout/config.json` to change any of this, or set
`"checkpointEnabled": false` to keep only the bailout.

Neither threshold guarantees capacity for another response. A single large
operation, or other activity on the account, can spend the remainder first.

`node ${CLAUDE_SKILL_DIR}/scripts/bailout.mjs status ${CLAUDE_SESSION_ID}` reports
the current reading, the thresholds in force, and the stage. Missing or stale
telemetry reports as unknown, never as zero.
