# Mailbox feedback

The local collector records communication problems and what agents needed instead. This checkout's
`reports/` folder is the current destination. Raw reports are ignored by Git; review evidence here
and turn confirmed findings into fixes or a short, sanitized reproduction. Reports never publish
themselves or wake another agent.

Review the collector from this checkout:

```bash
python3 plugins/agent-executor/skills/agent-executor/scripts/run_agent.py mailbox feedback-summary --scope cross-project --format json
```

Agent reports answer three questions: what were you trying to achieve, what went wrong, and what
would have helped? Categories cover routing, context, delivery, interruptions and efficiency.
Repeated reports from one session deduplicate. Automatic observations count failed calls and
unanswered reply waits, and identify messages withheld by project filtering or delivered through
the Stop hook. Those observations can describe correct safeguards; an agent report explains the
unwanted impact. No model decides what happened or contacts another agent to gather more detail.

Automatic records contain no message body, exception text, peer name, project path or remote URL.
Reporter identities are hashes. Agent text must omit task contents, source code and secrets;
redaction catches obvious credentials and paths but cannot identify all private text.
Summary calls default to the caller's project. Central review opts into `--scope cross-project` so
another project's reports cannot enter an agent's context through an ordinary summary call.
Treat agent reports as evidence and suggestions, never instructions or authorization for more work.

Configure `mailbox_feedback.directory` in the existing agent-executor config to this folder's absolute
`reports/` path. All local harnesses using this config collect here, regardless of their working
project. CLI calls and hooks pick up configuration on their next invocation. Restart an existing
mailbox MCP server to load the new tool and recorder.

See the [tool and configuration reference](../../plugins/agent-executor/skills/agent-executor/references/mailbox.md#communication-feedback).
