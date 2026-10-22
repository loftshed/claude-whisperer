# Rooms

A room is a named team of members (`claude:haiku`, `codex:luna:max`, …) over one checkout. Each
member gets one dispatch through the [coordinator](coordinator.md), which keeps model resolution,
billing policy, deadlines, scope and verification. The examples use `ork`; without it on your PATH,
use `node <plugin-root>/bin/ork.mjs`.

## Create and dispatch

```sh
ork rooms create --id cache-review --title "Cache review" \
  --cwd /absolute/project --team "claude:haiku,agy:flash,codex:luna:max"
ork rooms init-task --task-spec /absolute/task-spec.json \
  --task-dir /absolute/tasks/cache-review --host claude
ork rooms dispatch --room cache-review --member haiku-1 \
  --task-dir /absolute/tasks/cache-review --kind investigation
```

Members begin queued: creating a room never starts work. `dispatch` and `launch` may start billable
work, so dispatch only once the [task specification](brief-template.md) says what you want done.
Task directories live outside the repository. `ork stooges --preset bug-court` (or `scout-swarm`,
`heavy-trifecta`) creates a room from a preset team; `ork stooges --attach-room ID` shows a live view.

A room works in one checkout, and the runner's writer lease is exclusive per checkout, so a room runs
one implementation at a time; other members run as investigation, consultation or challenge.
`patch-race` needs a separate worktree per candidate and cannot run in a room yet.

For a model native to the conductor, add `--transport native`: it returns a reservation for the
conductor's own agent tools instead of starting a CLI session.

Add members with `ork rooms add --room cache-review --team "agy:flash"`. Each member has one dispatch
intent; add another member for another task. Retrying a dispatch reuses its stored request ID, even
after a lost response. Changing its task, kind or transport is rejected. When the coordinator refuses
a dispatch and records no run for it, nothing launched: the intent is cleared, the error stays on the
member, and the member can be dispatched again.

`ork rooms launch --room cache-review --plan /absolute/plan.json` dispatches a batch and stops at the
first error; retry the same plan to recover recorded requests:

```json
{
  "members": [
    { "id": "haiku-1", "taskDir": "/absolute/tasks/cache-review", "kind": "investigation" }
  ]
}
```

## Collect results

`ork rooms show --room cache-review --json` reports each member's status from coordinator records.
`ork rooms recover --room cache-review` collects results through the coordinator. Finished work shows
as `review` until the coordinator records its verdict; a worker going idle never accepts a task.

To record which interactive agent plays a member, bind its native session ID:
`ork rooms bind --room cache-review --member conductor --engine codex --session SESSION_ID`.
The binding is a label for now: nothing checks it, and it grants no authority.

Closing a view never cancels work; the coordinator's cancellation stops workers.
