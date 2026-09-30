# Completion delivery

Keep waiting outside the language-model loop. Repeated status commands, sleeps, log tails, or
"is it done?" checks waste context even when each check is cheap. Prefer these modes in order.

1. **Native host supervision.** Run the executor in foreground mode, without `--detach`, through the
   caller's background-command or task primitive. The host owns the process and delivers its normal
   completion signal. Also supervise the printed `AGENT_CHANNEL` with one
   `messages wait CHANNEL --recipient conductor` so a worker question cannot wait unseen until timeout.
   Answer/acknowledge messages, then wait again. On an Antigravity host, let its command runner move the foreground command to
   an asynchronous task; do not nest `--detach` underneath it merely to wait.
2. **Portable blocking signal.** When native lifecycle delivery is absent or cross-caller handoff
   matters, launch with `--detach`, keep the returned `AGENT_EVENT`, and make exactly one blocking call:

   ```bash
   python3 <skill-directory>/scripts/run_agent.py events wait <AGENT_EVENT-path-or-id> --messages --timeout 30m
   ```

   The process does cheap filesystem checks and returns on a worker message or the atomic terminal event.
   It never invokes a model. Reply to questions or acknowledge updates, then wait again for the same
   event. The [communication guide](communication.md) gives the exact commands and terminal-message rules.
   Set the wait timeout beyond the executor timeout. If the host
   yields while it is still blocked, continue that same process with long host waits (about 60 s).
   Do not start a concurrent `events wait`, inspect files, or ask a model for status while that waiter runs.

3. **Several jobs.** One process supervises them all:

   ```bash
   python3 <skill-directory>/scripts/run_agent.py events follow <event-1> <event-2> --messages --timeout 1h
   ```

   It deduplicates references and prints one JSON line per finished job. It returns early for a
   worker message; handle it and wait on the unfinished IDs again. Otherwise it exits when every
   job is terminal. A timeout exits nonzero and names the unfinished event IDs.

4. **Later-turn inbox recovery.** If no process can stay attached, rely on desktop/hook delivery and
   query the completion and message inboxes once when a later caller resumes. A worker needing an
   answer may time out and report BLOCKED if no conductor remains active.

`wait` and `follow` never acknowledge events. A steered job's superseded event carries
`superseded_by`, and both commands follow it to the job's final event. If `events follow` is interrupted, do not relaunch the
work: on the next turn list the inbox once, review every unacknowledged `result_path`, and acknowledge
only after review.

## Inbox

Before starting another delegated run in a repository, read its inbox once:

```bash
python3 <skill-directory>/scripts/run_agent.py events --cwd <absolute-repository-root> --format json
python3 <skill-directory>/scripts/run_agent.py events --ack <exact-event-id>
```

Detached runs keep private artifacts under `<cache>/jobs-v1` and atomically publish an event under
`<cache>/completions-v1` (`<cache>` is `~/.cache/agent-executor` or `$AGENT_EXECUTOR_HOME`), so a job
launched by one agent can be collected by another. Acknowledge only after the result and workspace
delta are reviewed.

## Detached launch details

`--detach` validates the model, effort, quota and worktree lease before it detaches, so a bad route
fails immediately instead of after "started". It returns `AGENT_JOB`, `AGENT_RESULT`, `AGENT_EVENT`
and `AGENT_EVENT_ID`, writes one durable inbox event, and sends a desktop notification by default
(`--notify none` for intentional silence).

A trusted adapter outside every executor workspace may be given with
`--completion-hook <absolute-executable>`. It receives the event path as its only argument plus
`AGENT_EVENT`, `AGENT_RESULT`, `AGENT_STATUS`, `AGENT_OUTCOME`, `AGENT_VERIFICATION_STATUS`,
`AGENT_ENGINE` and `AGENT_MODEL` in a restricted environment.

Desktop notification and inbox delivery cost no tokens but do not resume an ended chat turn; no
cross-vendor callback API exists. If same-turn review is mandatory and the host has no callback, use
one blocking `events wait --messages` at a time.

Foreground runs are silent except for coarse, process-derived `AGENT_LIFECYCLE` lines (executor
start/finish with changed-file count, verification start/finish, `quota_low`). `--heartbeat-seconds 60`
adds periodic progress when that is useful.
