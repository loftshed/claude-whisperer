# Changelog

## 0.14.0

- The launch gate, `quota` table, blocked-route alternatives and `coordinator.py recommend` treat an
  ai-usage pool with status `credits` (Codex once its weekly allowance is used up, with a credit balance)
  as usable: runs go ahead metered against the balance and print `AGENT_LIFECYCLE=quota_credits`; the
  pool ranks after routes with free quota. Exit 15 now means exhausted with no credits.

## 0.13.0

- feat(agent-executor): add OpenCode v2 support with version detection and caching
