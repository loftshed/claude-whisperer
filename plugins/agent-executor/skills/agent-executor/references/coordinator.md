# Conductor workflow and recovery

The host agent owns ambiguity, routing, evidence interpretation, and acceptance. The coordinator
records and enforces the execution contract. Use native host tools for native models and the
existing audited runner across providers. No worker or consultant grants new authority.

## Ownership, candidates, and limits

`coordinator.py route --packet route.json` takes `host`, `engine`, `model` and the conductor's
`context_portable`, `independent_acceptance`, `handoff_worthwhile` booleans. Without all three,
the owner continues. For a narrow consultation, provide `consultation: true`, `question`, and
`decision_needed`; ownership remains with the current agent.

`recommend --packet access.json` filters [profiles.json](profiles.json) using observed access
records: `model`, `engine`, `available`, `observed_at`, `source`, and `billing_mode`. The packet
also has `host` and `role`. Use native availability first; do not run external discovery for a
native route. Candidates are provisional, and effort suggestions still require validation by
the selected harness. Public API availability does not establish subscription entitlement.
Change authentication profiles or billing context only with explicit user authorization.

## Initialize and dispatch

Create the normal authoritative nine-field task spec. Keep the spec and private task directory
outside the repository. No clean-worktree requirement is added.

```bash
python3 scripts/coordinator.py init --task-dir /private/task \
  --task-spec /private/spec.json --host codex --time-budget 45m --invocation-allowance 5
python3 scripts/coordinator.py dispatch --task-dir /private/task \
  --kind implementation --request-id implement-1 --engine agy \
  --model gemini-3.8-flash-medium --effort medium \
  --verify-command 'python3 -B -m unittest tests.test_jobs'
```

The measured allowance is supervised executor invocations, including failed attempts and
consultations. A reservation consumes allowance before launch. CLI automatic retries are disabled.
Uncertain reservations are never refunded on the assumption that nothing ran. Usage, API reference
estimates, native estimates, latency, and unknown provider quota are recorded separately in
`cost_ledger`. Invocation quota and wall time are enforceable; they are not a hard invoice or
provider subscription-quota ceiling. Neither installed CLI exposes a verified hard dollar cap.
Do not claim otherwise or silently switch accounts/SDKs to obtain different controls.

The deadline bounds generation and remaining checks. Discovery and final audit/persistence may
finish afterward. Three consultation calls total, including one challenge, and one correction are
allowed within the task allowance. Exhaustion returns work to the conductor, without automatically
asking the user. The conductor can investigate or narrow the task within existing authority.

External dispatch is foreground. Use the host's supervised process handle and one wait. Reusing
an exact request ID returns the reserved run; it never starts another process. The state is
atomically persisted before and after dispatch. The worktree lease survives a runner parent exit
while an inheriting executor or check process still runs.

## Native host bridge

Use `dispatch ... --transport native` with the native engine/model. This reserves the request and
starts an audit guard; it does not wrap or invoke another model CLI. When the native tool inherits
host settings, use `--model host-default` and omit effort rather than inventing resolved settings.
An explicit model/effort must match what the host actually sends to its tool.

1. Read the returned `brief_path`, deadline, run ID, and event ID. The guard holds the worktree lease
   and snapshots dirty paths, ignored deliverables, HEAD, refs, reflog, stash, and worktrees.
2. Invoke the native tool once, within the original delegation authority. Immediately persist its
   exact handle with `native-attach --run-id ID --packet handle.json`, where the packet contains
   `native_handle`. Keep the native tool as the single supervision owner.
3. Wait through the native tool. At the task deadline, interrupt that exact tool using its native
   cancellation facility and collect its terminal result. The guard deliberately retains the lease
   until terminal evidence arrives; expiry alone cannot prove a native tool stopped. `next` returns
   an `interrupt` action for an overdue active run. Do not leave native work unsupervised.
4. Save the actual five-heading report outside the worktree. Call `native-complete --run-id ID
   --packet receipt.json`. The receipt requires `native_handle`, the actual host `event_id`,
   `terminal: true`, `tool_status` (`completed` or an honest failure), and `report_path`.
   Include `observed_model`/`observed_effort` only when the native tool actually reports them.
5. The guard checks scope/history, runs the declared independent checks once, checks for check
   mutations, and saves the additive result.v2 contract. The task still needs conductor review.

Native correction requires the original handle and workspace. Native output can be completed while
the underlying task remains blocked; the report contract determines that distinction. The native
host event and the coordinator's completion event are both retained. Only the coordinator event
has an inbox acknowledgment; native supervision is not duplicated by another polling agent.

## Bounded consultation and adaptive next actions

A blocked/failed worker may put one fenced `consultation-request` JSON block under `RISKS OR
BLOCKERS`. It contains `question`, `decision_needed`, and string arrays `observations`, `attempts`,
`versions`, `hypotheses`. Include an observation and a hypothesis. It requests advice only.

Finish the original audit before `dispatch --kind consultation --packet request.json`. The
coordinator derives an empty mutation scope. Challenge only a concrete weakness with `--kind
challenge`: missing source, wrong version, conflicting observation, or untested assumption.
Prefer a discriminating test over another vote. Never launch nested peer CLIs inside an audited job.

`next --task-dir DIR [--packet observation.json]` returns the next host action. Observations can
include `uncertain_fact`, `version_conflict`, `stale_evidence`, `weak_answer`, `discriminating_test`,
`question`, or a verified `review_id`. It chooses evidence retrieval, consultation, challenge/test,
resume, review, wait/recovery, interruption, or conductor takeover. Only a concrete
`authority_missing` observation asks the user; ordinary technical uncertainty stays with the team.

## Pointdexter: capture before review

Register actually discovered skills, instructions, and documentation tools with `register --packet
resource.json`. A resource has `kind` (`skill`, `instruction`, `doc_tool`), `name`, `applicability`,
and either an absolute `path` or the actual callable `tool` name. Registration grants no authority.
Read applicable resources and preserve repository instructions; current docs can correct stale
technical advice without discarding repository constraints.

Capture sources with `capture --source-kind KIND --component NAME --version VERSION` and exactly
one of `--url HTTPS_URL`, `--path LOCAL_PATH`, or `--command NON_MUTATING_CHECK`. Use `--authority
official` for verified official sources and `--updated-at` when a publication date is available.
The host fetches/copies actual bytes and records location, redirect, timestamp, hash, and version.
Command checks capture real stdout/stderr, exit code, and workspace/history before and after.
Changed work or a failed check cannot supply a successful evidence receipt.

Capture installed versions from actual version output or lock/config files with source kind
`version` or `config`. For already passed runner gates, use `capture-checks --run-id ID --component
NAME --version VERSION`; it imports the existing actual outputs without rerunning passed checks.

Then use `verify-evidence --packet review.json`. The packet requires:

- `question`, `conclusion`, `owner`, and `invalidation_trigger`.
- `versions`: component-to-version mapping, and `version_sources`: component-to-captured-source-ID.
- `claims`: each has `kind` (`fact`, `inference`, `observation`), `statement`, and `citations` containing
  a `source_id` and an exact supporting `excerpt` from its captured content.
- For acceptance, `criteria`: every original criterion in order, each with `criterion` and
  `claim_indexes`. Record unresolved `gaps` as an array; acceptance requires none.
- When a current fact is disputed, `disputed_since`: its actual timezone-aware timestamp. Sources
  predating that question must be fetched again even within the 24-hour cache window.

The host verifies real excerpts, artifacts, check outputs, installed-version observations, freshness,
and changed local files. Version/config/source changes invalidate prior records. The conductor must
still judge whether the content supports each inference and whether a URL is authoritative; hashes
and matching excerpts are not semantic proof. A worker cannot mint a receipt by setting a flag.

## Review, correction, decisions, and acknowledgment

New tasks require the persisted host review ID. `review --run-id ID --decision supported --review-id
ID` records supported advice. `dispatch --kind correction --request-id repair-1 --review-id ID`
inherits the original engine, model, effort, variant, billing, session, scope, and checks. Add
`--transport native` for a native continuation. No unchanged failed prompt or safety-violation
resumption is allowed. Original failed/blocked outcomes remain unchanged.

`review --run-id ID --decision accept --review-id ID` requires the latest completed audited work,
all original criteria, unchanged workspace/history, and current evidence. `reject` records failed
review; `takeover` returns finished work to the conductor. `ack --run-id ID --event-id EXACT_ID`
requires a durable review first and acknowledges only that result's completion event.

When a specific problem in a verification command has been corrected, `reverify --run-id ID
--request-id CHECK_ID --reason EXPLANATION [--verify-command COMMAND]` records a separate host-only
verification phase. It requires unchanged audited work, remaining time, and a completed executor
report; it cannot repair scope/history violations or a failed/blocked worker. It does not launch a
model or spend another invocation. Its new result can support acceptance while the original failed
check stays failed. Repeat passed checks only for a concrete change or concern.

Use `record-decision --packet decision.json [--review-id ID]` for reusable knowledge separately
from run progress. Include `question`, `chosen_action`, `applicability` with versions, an array of
`rejected_alternatives` (`alternative`, `why`), `owner`, `invalidation_trigger`, and `reusable`.
Reusable conclusions require current evidence. Keep failed approaches; do not canonize unverified
claims or stale decisions.
`knowledge` revalidates dependent evidence and marks obsolete entries invalid before reuse.

After interruption, run `recover` once. It imports an existing terminal result, checks recorded
process birth identity and native guard checkpoints, or preserves uncertainty. An active handle
remains active. Missing results or reused PIDs never justify launching a replacement. Recover the
native tool from the host's durable event/handle history if a crash fell between dispatch and attach.
Neither adapter claims exactly-once remote execution across that uncertain crash window.

Final-state auditing detects final violations. It does not prevent transient/external side effects,
other editors, or Git-ref changes from another worktree. Existing executor permissions remain intact.

## Calibration

`calibration.py init --directory DIR --packet manifest.json` fixes 12–20 representative real tasks,
starting revisions, acceptance criteria, context packets, at least three held-out tasks, and two
repeats. Run `current_owner`, `fresh_worker`, and `owner_plus_consultant` through the host and retain
all attempts. `record` validates task/result receipts; `summarize` keeps missing measurements null.
Record context setup, accepted outcome, regressions, missed constraints, review false positives,
all usage, latency, repairs, reconstruction time, and quota separately. Review held-out outcomes and
variance before promoting a route. A guidance comparison or fixture pilot is not a measured model
ranking for a production repository. See [design-completion.md](design-completion.md) for status.
