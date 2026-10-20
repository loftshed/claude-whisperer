# Orkest swarms in Herdr

The optional Herdr plugin groups Orkest rooms into workspaces and tabs. Orkest owns
members, dispatch requests, task results, and review decisions. Herdr owns terminals
and navigation. Native subagents and background workers appear in room views without
extra terminal panes.

## Install

Requires Node.js 22.18 or newer, Python 3.11 or newer, and Herdr 0.9.5 or newer.
No additional npm packages are needed at runtime.

From this checkout:

```sh
herdr plugin link "$PWD"
```

Link the repository root. Its `herdr-plugin.toml` includes entrypoints that use both
orchestrate and monitor. Linking only an orchestrate subdirectory will not work.
Installation is explicit and does not start workers or change model accounts.

Inside a Herdr workspace, open the overview:

```sh
herdr plugin action invoke orkest.swarms.overview
```

An optional Herdr keybinding:

```toml
[[keys.command]]
key = "prefix+o"
type = "plugin_action"
command = "orkest.swarms.overview"
description = "Orkest swarms"
```

The examples below use `ork`. If it is not on your PATH, substitute
`node /absolute/path/to/orkest/plugins/orchestrate/bin/ork.mjs`.

## Create and run a swarm

```sh
ork rooms create --id cache-review --group Reviews --title "Cache review" \
  --cwd /absolute/project --team "claude:haiku,agy:flash,codex:luna:max"
ork herdr open --room cache-review
```

Members begin queued. Creating or reopening a view never dispatches them.
The group identity includes the exact checkout, so equally named groups in separate
worktrees remain separate workspaces.

Initialize a task with the existing
[task specification](../plugins/orchestrate/skills/orchestrate/references/brief-template.md)
and [coordinator contract](../plugins/orchestrate/skills/orchestrate/references/coordinator.md).
Task directories must be outside the repository:

```sh
ork rooms init-task --task-spec /absolute/task-spec.json \
  --task-dir /absolute/tasks/cache-review --host codex
ork rooms dispatch --room cache-review --member haiku-1 \
  --task-dir /absolute/tasks/cache-review --kind investigation
```

Dispatch uses the existing coordinator, including model resolution, billing policy,
deadline, scope, and verification checks. An implementation task still needs its
normal acceptance checks. The plugin does not silently select a different account.
Only dispatch once the task specification reflects the work you want done.

For a model native to the conductor, use `--transport native`. This returns a
reservation for that conductor's native agent tools. The plugin does not create
a second CLI session for native work.

To add members, use `ork rooms add --room cache-review --team "agy:flash"`.
Each member has one dispatch intent. Add another member for another task.
Retrying the same dispatch reuses its stored request ID, including after a lost
response. Changing its task, kind, or transport is rejected.

For an explicit batch, `ork rooms launch --room cache-review --plan /absolute/plan.json`
accepts:

```json
{
  "members": [
    {
      "id": "haiku-1",
      "taskDir": "/absolute/tasks/cache-review",
      "kind": "investigation",
      "transport": "executor"
    }
  ]
}
```

The batch stops at the first error. Retry the same plan to recover recorded requests.
Coordinator restrictions on parallel work still apply.

## Navigate and collect results

The overview uses Up/Down or `p`/`n` to select a swarm.

| Key   | Action                                              |
| ----- | --------------------------------------------------- |
| Enter | Open or focus the selected swarm's detail pane      |
| `f`   | Focus its explicitly bound coordinator session      |
| `v`   | Filter Herdr's Agents view to the selected swarm    |
| `c`   | Create a queued swarm                               |
| `a`   | Add queued members                                  |
| `d`   | Initialize or select a task, then dispatch a member |
| `r`   | Ask the coordinator to collect available results    |
| `q`   | Close the overview without stopping workers         |

The dispatch form confirms before starting potentially billable work. CLI dispatch
commands act immediately.

For interactive agents already running inside Herdr, bind the native session ID:

```sh
ork rooms bind --room cache-review --member conductor --engine codex --session SESSION_ID
ork herdr focus --room cache-review --member conductor
```

Use a worker's member ID to bind an interactive teammate. Matching requires the
native session ID, agent engine, local machine, and exact checkout. A duplicate
or missing match remains ambiguous or unbound. Reconciliation does not move
someone else's panes or launch replacements.

`ork rooms list --json` and `ork rooms show --room cache-review --json` work without
Herdr. `ork rooms recover --room cache-review` collects results through the coordinator.
Finished work appears as `review` until the coordinator records its verdict.
Herdr's `idle` or `done` state never accepts a task.

The overview reads cached quota; it does not query providers or change billing
context. Missing cache data remains unavailable.

## Recovery and boundaries

Commands that target Herdr require `HERDR_SOCKET_PATH` from that session.
They never search for or fall back to another focused session.
The adapter is local to a machine and socket; remote-machine orchestration is not
implemented.

The overview refreshes metadata while it is open. Worker metadata expires after
15 seconds without refresh. `ork herdr clear-filter` clears only the filter owned
by this plugin; it leaves a different plugin's active filter alone.

Layout bindings live in the mailbox's `herdr` directory so plugin actions and direct
CLI commands share them. Room/task state remains in Orkest. A moved detail
pane can be found from its room metadata; interactive agents are matched by session.
Restored detail processes republish their room metadata. A missing or ambiguous
view never causes an automatic worker restart.

If a layout request loses its response, inspect the workspace before running
`ork herdr forget --room cache-review`. This clears local layout intent, not the
swarm or its workers. The next open can reuse a uniquely tagged existing view.
After a process crash, an empty `.lock` directory may remain next to adapter state.
Confirm its owning command has exited before removing that exact empty directory.

Disable or unlink the plugin with Herdr's plugin commands when finished. Closing,
disabling, or unlinking the views does not cancel coordinator tasks. Existing native
and executor cancellation mechanisms remain responsible for stopping work.
