# Peer Mailbox Token-Efficiency Observations

## 1. CLI Discovery Overhead (`mailbox` Subcommand Not Listed in Top-Level Help & Flag Naming)

- **What we were trying to achieve:** Register a session, list active peers, and exchange messages with a peer agent in another worktree of the same repository.
- **What went wrong:** `SKILL.md` only mentions `peer-mailbox` by name and points to `references/mailbox.md` for details. Running `run_agent.py --help` does not surface `mailbox` or `feedback` in the top-level subcommand summary unless the agent either reads `references/mailbox.md` (an extra ~260 lines of context) or inspects the CLI parser directly. Furthermore, agents frequently guess `--body` or `--message` on `mailbox send` instead of `--text`, causing avoidable failed CLI invocations before reading the subcommand `--help`.
- **What would reduce token waste:**
  1. Include a 4-line quick-reference block directly in `SKILL.md` showing the exact CLI syntax for `register`, `peers`, `send --session <id> --to <peer> --text "..."`, `inbox --session <id>`, and `ack --session <id> --ids <msg-id>`.
  2. Accept `--body` and `--message` as aliases for `--text` on `mailbox send`.

## 2. Worktree Branch Divergence Causes Incorrect Routing / Extra Clarification Turns

- **What we were trying to achieve:** Receive review findings from a peer coordinator working in the same repository.
- **What went wrong:** Because `mailbox peers` displays each peer's current worktree directory and branch name, the coordinator saw a different branch name on our session (`gemini:<worktree-branch>`) and hesitated to send the findings until the user manually intervened, assuming we were the wrong agent.
- **What would reduce token waste:**
  - Allow `mailbox register` to include a short human-readable `--role` or `--Capabilities` note (or surface the last status summary in `mailbox peers`) so an idle/listening agent can advertise `"awaiting review findings from coordinator"` regardless of which worktree branch it is sitting on.

## 3. Polling Loop vs. Blocking `receive --wait-seconds`

- **What we were trying to achieve:** Wait for a peer agent to finish compiling its findings and send them over the mailbox.
- **What went wrong:** Without an event-driven wakeup from the external SQLite mailbox into the host harness, using short timers or repeated `mailbox inbox` checks burns multiple planner turns and context tokens.
- **What would reduce token waste:**
  - Prefer a single blocking `run_agent.py mailbox receive --session <id> --wait-seconds 120` background command (which polls SQLite locally in Python at zero LLM token cost and exits only when a message arrives or the timeout expires), so the host harness wakes up once when the command completes.
