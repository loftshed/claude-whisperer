# Live conductor and worker communication

Each new runner invocation creates a private `channel.json` beside its result. The runner prints
`AGENT_CHANNEL`, passes its path as the executor's `AGENT_CHANNEL` environment variable, and adds
the communication commands to the submitted brief. Coordinator runs return `channel_path`;
native dispatches put the same instructions in `brief.md`. Existing running jobs do not acquire
this protocol retroactively.

The channel supports questions, replies, and updates in both directions. It uses file locking and
atomic replacement, retains history, and deduplicates sends by caller-supplied message ID. Reusing
an ID with different content is an error. Replies name the exact question and acknowledge it.
Messages are local collaboration data, not evidence verification or permission to expand scope.
Roles identify cooperating agents; they are not an authentication boundary between local processes.

## Supervise a run

For a detached or coordinator run, wait for either a worker message or completion:

```bash
python3 scripts/run_agent.py events wait EVENT_ID --messages --timeout 30m
```

An `agent-executor.message-signal.v1` result contains `channel_path`, the outstanding worker
messages, and `run_status`. Null `run_status` means the run has not finished or reached its deadline.
Answer a question while the run is active:

```bash
python3 scripts/run_agent.py messages send /private/run/channel.json \
  --sender conductor --id answer-1 --kind reply --reply-to question-1 \
  --text 'Use the existing cancellation contract; the trace is in the supplied evidence.'
```

For updates, act on the information and then acknowledge the exact message:

```bash
python3 scripts/run_agent.py messages ack /private/run/channel.json \
  --recipient conductor --id finding-1
```

Then start the next blocking `events wait ... --messages`. This is an event-driven response cycle,
not repeated status polling. Never leave two waiters supervising the same event. For several runs,
`events follow EVENT_1 EVENT_2 --messages` emits completed events until a message needs attention,
then returns. Keep the unfinished event IDs for the next call; completion events are otherwise
replayed if supplied again. No event or message is silently acknowledged by a wait.

Foreground runs without a completion event can use
`messages wait CHANNEL --recipient conductor --timeout 300`, which returns on messages, completion,
deadline, or timeout. The channel path is printed before executor dispatch. The process supervisor
still owns the executor lifecycle. A waiting tool does not resume an already ended host chat turn.

`coordinator.py next` returns `respond` for live worker messages or `review_message` for messages
left by a finished run. `show` and `next` remain readable during foreground dispatch. Do not send a
late answer to a finished run: review and acknowledge the message, then use a supported continuation
or take over. A timeout does not imply that the worker received or agreed with anything.

## Worker questions and conductor questions

The worker can ask and wait for the matching answer in one tool call:

```bash
python3 scripts/run_agent.py messages ask CHANNEL --sender worker \
  --id question-1 --text 'Which client version produced the cancellation trace?' --timeout 300
```

The same command with `--sender conductor` asks the worker a question. `send --kind update`
queues information without waiting. Workers read `messages inbox CHANNEL --recipient worker`
at meaningful checkpoints and before their final report. A `send` means queued, not read or acted
upon. Use a reply or acknowledgment to establish receipt. Messages do not interrupt generation;
use `steer` for an urgent detached-job direction change. Steering retains the original channel.

If an answer is missing, continue independent work when possible. Otherwise return BLOCKED with
the question. Do not turn unanswered questions into assumed approvals or launch nested peers.
Ask the conductor to obtain peer advice and relay the result.

Waits use no model calls and are bounded by both their timeout and the run deadline. Exit 12 means
timeout; exit 30 means the channel closed without a matching message. An answered question can be
replayed after reconnecting. `messages history CHANNEL` returns all messages and acknowledgments
as JSON for inspection or a future UI. Messages are limited to 16 KiB each and 1,024 per run.
