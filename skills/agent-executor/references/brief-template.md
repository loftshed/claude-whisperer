# Execution brief template

Prefer the bundled task spec over copying this template into chat or manually recreating its shared
guardrails. The runner renders the complete standalone brief privately while the conductor writes
only the task-specific values.

Create a concise JSON spec outside the target repository and pass it directly to the runner:

```bash
python3 <skill-directory>/scripts/run_agent.py \
  --cwd <absolute-repository-root> \
  --task-spec <brief-spec.json> \
  --engine agy \
  --model gemini-3.8-flash-medium \
  --effort medium \
  --expect-changes
```

`--task-spec` is the authority for the repository root and path scope. The runner rejects a
root/CWD mismatch and any duplicate `--allow-path` or `--track-path` flags, then derives the
complete scope from the normalized JSON. If scope changes, update the JSON; do not hand-edit a
rendered Markdown preview or retype the scope as runner flags. Use `render_brief.py --spec ...`
only when you want a human-readable preview before dispatch.

```json
{
  "objective": "One concrete outcome.",
  "repository_root": "/absolute/path/to/repository",
  "current_state": "Short factual summary of relevant implementation.",
  "pre_existing_changes": ["src/already-dirty-file.ts"],
  "plan": [
    "Specific implementation step.",
    "Specific test or documentation step."
  ],
  "allow_paths": ["src/jobs", "tests/jobs"],
  "track_paths": ["plans/generated-handoff.md"],
  "development_checks": ["python3 -m unittest tests.test_jobs"],
  "acceptance_criteria": [
    "Observable behavior or test result.",
    "Compatibility or regression condition."
  ]
}
```

All fields are required. Arrays may be empty only for `allow_paths`, `track_paths`,
`pre_existing_changes`, and `development_checks`; an empty allowed/track scope is an explicitly
read-only brief. Paths in `allow_paths` and `track_paths` must be non-root repository-relative
paths. The runner derives its effective allowed scope from exactly those two arrays, so scope cannot
drift from the submitted brief or runner enforcement.

An optional `context_packet` object accepts four string arrays: `decisions`, `failed_attempts`,
`evidence`, and `versions`. Include only context needed to finish without unrecorded history.
The nine fields above remain required. Keep native reasoning and opaque tool state in their own
session. For a narrow read-only question, see [coordinator.md](coordinator.md).

The renderer includes the mutation budget, shared constraints, development-check guidance, and the
ordered five-heading report contract. It does not embed runner-owned `--verify-command` gates;
keep those exact non-mutating commands on the runner invocation.

Direct handwritten briefs remain supported for exceptional cases. They must still be standalone,
match the runner path scope exactly, and comply with the report contract.

## Brief-writing checks

- Name real paths, symbols, and commands after inspecting the repository.
- State what must remain unchanged; execution agents otherwise tend to broaden cleanup.
- Separate command-level evidence from broader completion claims; a passing focused check does not complete unrelated gates.
- Keep deterministic acceptance gates in runner `--verify-command` arguments. The executor may use narrower development checks; the runner observes final gate exit codes outside the model.
- Keep the final-response headings identical to the runner contract above. The runner also injects
  this contract, but contradictory headings in a brief can make an otherwise correct run fail as
  `malformed_report`.
- Pass only non-mutating commands to `--verify-command`; the runner treats a final-state verifier mutation as failure.
- Never place secrets in `--verify-command`; exact command strings are retained in private run artifacts.
- For a `--task-spec`, treat `allow_paths` and `track_paths` as the one source of truth; do not pass duplicate runner scope flags.
- Track every ignored deliverable explicitly; Git status cannot prove that it was created.
- Prefer one coherent batch over several conversational micro-delegations.
- Put bulky context in workspace files and point to them rather than pasting it into the brief.
- Never place a secret in the brief. Its contents are sent to the selected model provider; the agy engine also includes the submitted brief in the local process arguments.
- Freeze a shared worktree while a runner job is active. Read-only jobs are audited just as strictly as writers, so concurrent edits are intentionally reported as scope/history violations rather than guessed away.
