# Collaboration review, 2026-09-28

This review covers the local runner, coordinator, native bridge and task briefs. The changes are
in the working checkout, which the installed skill hub links to. No provider model was invoked.

## Gaps addressed

| Gap                                                                                 | Change                                                                                                                      |
| ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| A worker could only request consultation in its terminal BLOCKED/FAILED report      | A per-run channel carries questions, replies and updates while the existing executor continues                              |
| Completion-only waits could leave a worker question unseen                          | `events wait/follow --messages` returns for messages as well as completion; ordinary foreground runs expose `AGENT_CHANNEL` |
| Foreground dispatch held the task lease and prevented status/next-action inspection | `show` and `next` read atomic snapshots without taking that mutation lease; message writes use their own lock               |
| All workers took exclusive worktree leases                                          | Empty-scope readers share a lease, while writers remain exclusive                                                           |
| Coordinator dispatch serialized even independent research                           | Background dispatch returns a durable handle and permits overlapping read-only kinds                                        |
| Consultation required a known narrow question and hypothesis                        | Investigation mode lets the worker choose and revise its approach within the objective                                      |
| Consultation and correction could discard task context                              | Original decisions/evidence survive; later briefs point to prior finished results                                           |
| Coordinator rejected Claude CLI runs after the runner gained Claude support         | Cross-provider Claude dispatch now uses the installed adapter; native-host rules still apply                                |
| OpenCode could not identify itself as the coordinator host                          | OpenCode is recognized as a native host                                                                                     |

## Remaining boundaries and follow-up work

- Messages are cooperative tool calls. A worker sees conductor updates at checkpoints or while
  waiting for an answer. Queuing a message does not prove it was read. Urgent detached-job changes
  still use steering. Native messages outside this channel are not automatically mirrored.
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
- Adapters and communication were exercised with deterministic local executor processes. This
  verifies transport and audit behavior, not whether every provider/model consistently follows
  the new checkpoint instructions. A paid cross-provider trial was not performed.
- The live UI is still future work. `messages history` provides explicit exchanges and receipt
  timestamps for it, but does not expose private reasoning or every tool event.
