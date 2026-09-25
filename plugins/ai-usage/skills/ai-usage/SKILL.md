---
name: ai-usage
description: Check how much subscription quota is left on the user's AI accounts (Claude Code profiles, Codex/ChatGPT, Antigravity/Gemini) and where it is best spent. Use before delegating work to another agent, model or CLI (claude, plaude, codex, agy, agent-executor), when choosing between accounts, before a large or long-running task, when a "usage limit" or rate-limit error appears, or when the user asks how much usage is left.
---

# ai-usage

Quota is use-it-or-lose-it: capacity left in a window when it resets is gone. This skill finds the
account and model pool with the most unused capacity so work lands there, within the user's rules.

## Get the data

Prefer the MCP tools (server `ai-usage`):

- `recommend {family?: "claude" | "gpt" | "gemini"}`: ranked pools, best first, each with a route hint.
- `get_usage`: every window per account (5-hour, weekly, per-model) with % left and reset time.

Without MCP, run `ai-usage recommend --json` or `ai-usage json` (schema `ai-usage.snapshot.v1`).
Results are cached for about 3 minutes. Pass `refresh: true` only after heavy use, not on every step.

## Read it

- `status: "blocked"`: a window in that pool is exhausted; `blockedUntil` says when it refills.
- `availableNowPct`: the tightest window right now, usually the 5-hour window. Below ~20%, a long task
  may stall mid-way.
- `surplusPts`: weekly % left minus what even use until reset would leave. Positive means the account
  is under-used and that capacity is lost at reset unless spent; negative means it is being used faster
  than it refills, so conserve it.
- Percentages are relative to each account's own plan. Compare accounts by surplus, never by raw %.

## Act on it

1. **The ranking is advice, not authorization.** Only route to accounts the user allows for this work.
   Switching between work and personal accounts switches billing context. Follow the user's standing
   rules (for example, personal Claude only when the user asked for personal quota). Otherwise ask.
2. Among allowed pools, prefer the highest positive `surplusPts` whose models fit the task. Quota never
   overrides capability: do not send work to a weaker model just because it has quota to spare.
3. If the natural route is blocked, say so with its refill time, then offer the next allowed route or
   waiting, whichever the task tolerates.
4. For long or parallel work, check `availableNowPct` as well as the weekly surplus.
5. Report the choice in one line, for example: "Using Codex (+12 pts, 64% usable now); personal Claude
   is under-used but not authorized for this task."

## With agent-executor

agent-executor reads this data itself: `run_agent.py quota` shows quota per route (`codex`, `agy`
Gemini, `agy` Claude/GPT-OSS), the runner refuses to launch into an exhausted pool (exit 15), and
`coordinator.py recommend` with `"access": "live"` ranks candidates by it. The ranking breaks ties
between validated routes; it does not replace agent-executor's route table or live model catalog check.
