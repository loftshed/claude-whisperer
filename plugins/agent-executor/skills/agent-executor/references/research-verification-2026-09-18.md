# Verified adapter contracts

Checked on 2026-09-18. These are dated adapter observations, not model rankings or account entitlements.

- Codex 0.155.0 emits cumulative thread usage through `turn.completed`. Keep the last snapshot and use a same-session baseline for incremental usage. Missing counters remain unknown. The emitting implementation is the [pinned JSONL event processor](https://github.com/openai/codex/blob/rust-v0.155.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs).
- Codex effort is bound with `model_reasoning_effort` and validated against that model's live catalog. A command setting is not proof of backend-resolved effort. The [model protocol](https://github.com/openai/codex/blob/rust-v0.155.0/codex-rs/protocol/src/openai_models.rs) provides model-specific supported levels.
- AGY 1.2.6 supports structured output. A live adapter check found nested `event`/payload envelopes and uppercase terminal status. The included regression fixture preserves that shape with synthetic identifiers and counters. The [headless guide](https://antigravity.google/docs/cli/headless/) describes result and step telemetry, but its flat examples do not match every installed wire detail.
- AGY result snapshots and step updates are separate. Do not sum both or infer exclusive billing buckets from undocumented counters. Configuration model echoes do not prove resolved model/effort, and cross-process resume accounting stays unverified.
- OpenCode 1.18.31 reports exclusive input/cache buckets and separate visible/reasoning output. Deduplicate step identities and add separate reasoning exactly once. See the [pinned session implementation](https://github.com/anomalyco/opencode/blob/v1.18.31/packages/opencode/src/session/session.ts).
- No verified hard per-run dollar cap was established for the installed CLIs. Codex's experimental rollout budget reacts to received usage and may use provider-defined units; it is not a guaranteed invoice ceiling. See the [pinned budget implementation](https://github.com/openai/codex/blob/rust-v0.155.0/codex-rs/core/src/rollout_budget.rs).

The dated tariff registry links official model/pricing sources and expires estimates after its freshness window. Public API estimates, native estimates, subscription billing, and actual provider quota are separate facts. Never fabricate missing usage or silently change billing context.
