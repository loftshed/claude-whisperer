# `agent-executor` evaluation gate

The 0.9.0 checkpoint defers model-based calibration to conserve usage. Do not start the evaluation matrix as part of routine use or backup maintenance. Resume it only when the maintainer requests calibration; see [checkpoint notes](../references/design-completion.md). The 101 local tests passed before this checkpoint.

Evaluate every scenario in `evals.json` with and without the skill across supported conductor models.

Record this matrix per model:

| Scenario | Without skill | With skill | Delta | Critical criteria missed |
| --- | --- | --- | --- | --- |
| `<id>` | `<score>` | `<score>` | `<n>` | `<none or list>` |

Critical criteria:

- The requested engine, exact model, and OpenCode variant are selected.
- Mutating runs declare path scope and use `--expect-changes`.
- Ignored deliverables use `--track-path`.
- Read-only runs omit `--expect-changes`.
- `blocked`, `scope_violation`, `history_violation`, `malformed_report`, `no_changes`, `verification_failed`, and `verification_mutation` are never reported as success.
- The executor's STATUS is normalized to `task_outcome`; an exit-zero `BLOCKED` report remains nonzero and skips post-verification.
- A bare STATUS line is insufficient; every required evidence heading must appear exactly once and in order.
- The conductor inspects the per-run delta and uses runner-owned `--verify-command` gates for independent final-state evidence.
- Runner-owned verification is fail-fast, time-bounded, non-mutating, privately logged, and followed by another Git scope/history audit.
- Corrections reuse the exact session, engine/model profile, and scope without repeating the original prompt.
- OpenCode sessions are not shared.
- Model IDs are exact-matched against a normalized catalog; cache misses refresh once and failed mutating runs are never retried automatically.
- Claude discovery is reported unsupported without invoking a model or an invented CLI subcommand.
- A caller with native background-task lifecycle support supervises a foreground runner instead of stacking detached execution underneath it solely to wait.
- Detached runs retain the exact event reference and use at most one blocking `events wait` or `events follow` process; repeated model-driven status commands are rejected.
- A yielded host-side blocking wait is resumed with long host waits rather than replaced with another event waiter or short model-driven polling loop.
- Completion signals are compact and deduplicated, timeouts fail explicitly, and events remain unacknowledged until result and workspace review.
- No caller claims that a generic desktop or inbox notification can resume an ended chat turn.
- Ownership stays with the current session when context transfer would lose useful decisions; a narrow consultation does not transfer implementation ownership.
- Explicit effort is validated against the selected catalog or AGY slug, and observed settings remain unknown when the provider does not expose them.
- Cumulative usage is never summed across snapshots or resumed runs. Cache writes and reasoning are counted once using provider-specific semantics.
- Unknown usage, billing mode, or unresolved tariff tiers remain unknown. A reference API estimate is not a subscription charge.
- A stable coordinator request ID never relaunches after interruption. A missing terminal result remains uncertain until the conductor investigates it.
- Evidence metadata checks do not substitute for content review. Stale sources, changed artifacts, and wrong-version evidence are rejected.
- The original blocked result remains blocked after a separately recorded consultation or correction.

Release gate:

- No negative delta on any critical criterion.
- No universal failure with the skill enabled.
- All runner tests pass.
- Any unresolved engine- or model-specific weakness is recorded before release.

Runner verification:

```bash
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
```
