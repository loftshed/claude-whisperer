# Why these task archetypes exist

## Decision rationale

Route work by the kind of judgment it requires and the evidence available to check it. A task with a
precise transformation and cheap checks can be delegated differently from a task whose requirements,
architecture, or acceptance criteria still need interpretation. The conductor owns that distinction
and the final conclusions; a worker's fluent explanation does not establish its correctness.

The six archetypes separate recurring sources of difficulty. They are practical planning categories,
not an exhaustive taxonomy, mutually exclusive labels, or measured model rankings. For a mixed task,
identify the decision that dominates the risk, then delegate bounded parts with their own checks.

| Archetype                | Why it needs a distinct treatment                                                                                                                | Evidence needed before accepting the work                                                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `debug-invariants`       | The failure is visible but its cause is uncertain. Guessing a plausible patch can conceal the underlying defect.                                 | A reproduced failure, a causal explanation, and checks that exercise the relevant invariant and nearby cases.                                    |
| `structural-refactor`    | Many edits must preserve the same behavior across module boundaries. Local correctness can hide broken interactions.                             | Explicit behavior to preserve, dependency analysis, and checks across affected boundaries.                                                       |
| `ui-design-system`       | Correctness includes visual hierarchy, interaction, accessibility, and consistency. Passing code checks covers only part of the task.            | Concrete design constraints, rendered and interaction evidence, and review against shared component rules.                                       |
| `architecture-conductor` | The main work is choosing responsibilities, resolving ambiguity, and sequencing dependent tasks. Those choices determine what workers build.     | Stated assumptions, evaluated tradeoffs, a coherent plan, and a way to verify each delegated result.                                             |
| `mechanical-sweep`       | The transformation is already decided and repeated across locations. Interpretation should be limited enough that mistakes are easy to detect.   | Examples of the transformation, exact scope, and automated or otherwise inexpensive checks. Escalate exceptions instead of inventing new policy. |
| `independent-review`     | The task is to challenge a result against requirements, rather than continue the author's argument. Independence is about evidence and judgment. | Reproduced defects, checked assumptions, and specific acceptance gaps. A different vendor alone does not make a review independent.              |

This is the rationale used for the current guidance, not a reconstruction of undocumented author
intent. Commit [3f160c4](https://github.com/loftshed/orkest/commit/3f160c4608d04551a8592c6d9655ffeb0e61df98)
introduced the route defaults, strength hints, and telemetry without a recorded benchmark comparison.
The [design checkpoint](design-completion.md) also leaves calibration pending.

## Choosing workers without outsourcing judgment

Use [routes.default.json](routes.default.json) as a starting candidate list, subject to explicit user
choices, observed access, and billing authorization. Its exact model order has not been validated by
a local comparison. Do not infer that a named model leads an archetype because a benchmark proved it.

The working hypothesis is to spend more reasoning capacity where uncertainty or an undetected error
is costly, and use faster workers where a clear brief and strong checks bound the risk. A small file
can still require architectural judgment; a large edit can still be mechanical. Scope size alone
is not a routing rule.

For example, a worker can collect benchmark sources, trace code, and identify missing evidence. The
conductor should interpret that evidence and own a routing rationale. Delegating the writing does
not transfer responsibility for its conclusions. The same principle applies to architecture and
ambiguous design decisions.

The `tier_strengths` strings in [families.json](families.json) are qualitative hints displayed by the
route viewer. They are not measured scores and do not participate in selector resolution or telemetry
ranking. Keep the current descriptions separate from verified capabilities of a pinned model version.
[profiles.json](profiles.json) explicitly labels its guidance as workload hypotheses.

## What the local evidence can establish

[telemetry.py](../scripts/telemetry.py) groups recorded results by task archetype, engine, resolved
model, and effort. It reports accepted outcomes, first-pass outcomes, duration, and the lower endpoint
of a 95% Wilson interval for the accepted proportion.

The current reorder heuristic considers candidate slots with `available` or `credits` quota status,
at least three matching runs, and a Wilson lower-bound difference strictly greater than 0.05.
Candidates without that evidence retain their slots. These thresholds avoid reacting to every result;
they are not a statistical test that one model is superior. Three runs are little evidence, different
task difficulty can distort comparisons, and accepted outcomes depend on the quality of verification.

Matching can also use a recorded requested selector, so evidence for an alias may include more than
one resolved model version. Candidates without an explicit effort can aggregate matching effort
groups. Inspect those versions and settings before treating a displayed score as evidence about a
specific model. The current code does not correct for task selection bias or changing evaluators.

To validate a route change, compare candidates on the same fixed tasks and starting revisions, with
the same acceptance criteria and recorded model/effort/tool configuration. Include held-out tasks and
repeated runs. Report failures, recovery, elapsed time, and verification effort as well as successes.
Cost per independently accepted task and overlap in failures between vendors are useful proposed
criteria; the current reorder heuristic does not measure or optimize either. The
[calibration tooling](../scripts/calibration.py) has separate API-reference cost estimates, which are
not subscription invoices.

## Quota is a separate constraint

[conductor_policy.py](../scripts/conductor_policy.py) requires observed access, rejects observations
older than 24 hours, and excludes unauthorized personal subscription access. The launch-time
[quota check](../scripts/quota.py) checks pool availability again. Available quota does not prove that
a model fits a task, and a favorable score does not authorize a billing switch.

Monitor's `surplusPts` compares remaining weekly capacity with an even-use schedule. An `expiring`
window is at least a day long, in its final 20%, and has at least 5% remaining. These signals help use
capacity before rollover. The generic profile recommender orders low-quota pools last, then prefers
included quota over credits, expiring pools, native routes, and greater surplus in that priority
order. Archetype routing instead starts with its configured candidate order and uses the telemetry
gate above; it does not apply that generic quota sort. See
[Monitor's ranking rules](../../../../monitor/README.md#ranking).

## What the benchmarks contribute

These sources suggest evaluation tasks. They do not establish the current candidate order or prove
that these six labels are the right taxonomy. Source descriptions were checked on 2026-10-18.

| Source                                                                                                   | Useful evidence for a future evaluation                                            | What it does not establish here                                                                                                                                                                                       |
| -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [SWE-bench](https://www.swebench.com/SWE-bench/) and [Multi-SWE-bench](https://arxiv.org/abs/2504.02605) | Repository issue resolution, including multilingual tasks in Multi-SWE-bench.      | Broad architectural judgment or behavior-preserving refactoring quality.                                                                                                                                              |
| [SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/)                           | A human-screened subset; difficulty annotations can help stratify tasks.           | A generic, timeless "hard" score. Pin the subset and difficulty definition. The [later audit](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/) raises test-quality and contamination concerns. |
| [Aider Polyglot](https://aider.chat/docs/leaderboards/)                                                  | Coding exercises across six languages, with edit-format and correctness results.   | Preservation of a large application's behavior during a structural refactor.                                                                                                                                          |
| [Design2Code](https://arxiv.org/abs/2403.03163) and [FrontendBench](https://arxiv.org/abs/2506.13832)    | Screenshot reconstruction and interactive frontend generation respectively.        | Design-system coherence, accessibility, or product judgment without additional checks. Pin the specific paper and runnable dataset before using the results.                                                          |
| [RULER](https://arxiv.org/abs/2404.06654) and [OpenAI MRCR](https://huggingface.co/datasets/openai/mrcr) | Controlled long-context retrieval, tracing, aggregation, and disambiguation tasks. | Architecture quality or effective coordination merely because a model handles a long input.                                                                                                                           |

Different vendors may provide useful alternative approaches, but failure independence must be
measured with paired tasks. Treat cross-vendor review as a hypothesis to test, not an automatic
quality multiplier. None of the cited sources supplies a local calibration result for Orkest's current
model selectors.
