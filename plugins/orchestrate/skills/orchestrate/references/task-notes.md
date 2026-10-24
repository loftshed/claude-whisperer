# Current work notes

Keep one short card about the work you are doing. It survives your mailbox presence ending, so another
agent can see where you stopped. This is saved context, not permission to take over or proof that the
previous agent has stopped writing.

Use `focus` at task start and update it at meaningful checkpoints with optional fields:

- `done`: last verified result, at most 160 characters.
- `next`: the next concrete action, at most 240 characters.
- `blocker`: an unresolved dependency or question, at most 160 characters.
- `refs`: a commit, PR, file or log reference, at most 240 characters.
- `status`: `active`, `blocked` or `complete`.

The task title is limited to 200 characters. Owner, checkout, branch and update time are recorded
automatically. Keep the title stable while updating progress. Changing it starts a fresh card and clears
the old details. Omitted fields stay as they were for the same title; an empty string clears a field.

```text
focus(text="Repair export", done="Serializer fixed; 6 focused tests pass",
      next="Review PR #8", refs="PR #8, commit abc123", status="active")
tasks()
```

The CLI equivalents are `mailbox focus --text 'Repair export' --done '6 focused tests pass'
--next 'Review PR #8' --refs 'PR #8, commit abc123'` and `mailbox tasks`. Read notes on demand when
resuming or coordinating. They are not inserted into every hook or sent as messages, and updates do
not wake anyone. Lists are paginated and project-scoped unless `scope="cross-project"` is explicit.

Each session keeps only its latest card per project, replacing prior text. Notes expire 30 days after
their last update. An absent owner is shown as “not present”, never automatically marked complete or
available for takeover. Before continuing, verify the checkout, commit, dirty files and current
ownership. Use the existing team handover rules when the task is managed by a coordinator.

Save notes before a likely interruption and after useful progress. A sudden timeout can leave the
last checkpoint stale; no system can reconstruct work that was never saved.
