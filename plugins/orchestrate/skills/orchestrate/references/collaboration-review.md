# Collaboration review, 2026-09-28

This review covers the local runner, coordinator, native bridge and task briefs. The changes are
in the working checkout, which the installed skill hub links to. No provider model was invoked.

## Gaps addressed

| Gap                                      | Change                                                             |
| ---------------------------------------- | ------------------------------------------------------------------ |
| Worker questions required final reports  | Run mailbox carries questions, replies and updates live.           |
| Completion waits hid worker questions    | `events wait/follow --messages` wakes; output has `AGENT_MAILBOX`. |
| Dispatch blocked observation             | `show` and `next` read snapshots without the mutation lease.       |
| Workers held exclusive leases            | Empty-scope readers share; writers remain exclusive.               |
| Coordinator serialized research          | Background read-only runs can overlap.                             |
| Narrow questions were required           | Investigations can choose and revise their approach.               |
| Later runs lost context                  | Prior decisions and results stay in briefs.                        |
| Claude CLI runs were rejected cross-host | External Claude dispatch uses its adapter.                         |
| OpenCode was not a recognized host       | OpenCode is recognized.                                            |

## Remaining boundaries and follow-up work

- Messages are cooperative tool calls. A worker sees conductor updates at checkpoints or while
  waiting for an answer. Queuing a message does not prove it was read. Urgent detached-job changes
  still use steering. Native messages outside the run mailbox are not automatically mirrored.
- The host must remain available to supervise. A filesystem event cannot restart an ended chat turn.
  Waits, explicit acknowledgments and message history support reconnection; they do not supply an
  always-running conductor. Native guard interruption remains the host's responsibility.
- Read-only work has an empty mutation contract and a final-state audit, not an OS sandbox.
  Transient writes, ignored untracked files, outside-repository writes and external side effects
  are not comprehensively prevented. Concurrent readers must remain non-mutating; experiments
  needing writes require a separately scoped run or isolated workspace.
- Writers remain serialized per worktree. Concurrent implementation needs isolated worktrees and
  integration ownership; this change does not introduce automatic worktree creation or merging.
- Investigation is bounded by the existing task deadline and invocation allowance. Narrow
  consultations still have their separate limit. Expanding exploration does not remove budgets.
- Adapters and run messaging were exercised with deterministic local executor processes. This
  verifies transport and audit behavior, not whether every provider/model consistently follows
  the new checkpoint instructions. A paid cross-provider trial was not performed.
- The live UI is still future work. `mailbox history` provides explicit exchanges and receipt
  timestamps for it, but does not expose private reasoning or every tool event.
