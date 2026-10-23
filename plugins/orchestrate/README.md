# orchestrate

Cross-harness consultation, bounded task delegation, project-scoped peer messaging, team leadership, and multi-agent swarm rooms across Claude Code (`claude`), Codex (`codex`), Antigravity/Gemini (`agy`), and OpenCode (`opencode`).

- **Native-first routing:** Every hosted frontier model runs through its vendor's own CLI (`codex` for GPT, `agy` for Gemini, `claude` for Claude Code); `opencode` is used only for open-weight models on OpenRouter (sending a hosted model to `opencode` exits 17). Within the same harness and account, agents use their host's built-in subagent and messaging tools; `orchestrate` and `peer-mailbox` cross harness or account/billing boundaries.
- **Bounded execution:** Delegated runs hold a POSIX worktree lease, enforce allowed file scopes, verify that Git history (`HEAD`, branch, reflog, stash) is untouched by the worker, check live quota via [`monitor`](../monitor/README.md), and run deterministic verification gates outside the model after handoff.

Requires Python 3.11+, Node.js 22+ (for `ork` and `stooges`), `git`, and at least one installed and authenticated harness CLI.

## Install

Pick one way for Claude Code to load the skill so it does not appear twice:

```sh
# Option A: link the skill into every installed harness from a checkout
<checkout>/plugins/orchestrate/install.sh            # links ~/.agents/skills/orchestrate, writes config, installs mailbox hooks, runs doctor
<checkout>/plugins/orchestrate/install.sh --dry-run  # preview what would change

# Option B: use the Claude Code plugin for Claude, and link the remaining harnesses
claude plugin marketplace add <checkout>
claude plugin install orchestrate@orkest --scope user
<checkout>/plugins/orchestrate/install.sh --no-claude
```

`install.sh` points `~/.agents/skills/orchestrate` (and `~/.agents/skills/agent-executor` for compatibility) at `<checkout>`, links each detected harness's `skills/` directory to that hub, initializes `~/.config/agent-executor/config.json` if missing, installs the `peer-mailbox` MCP registration and turn hooks, and runs `doctor`.

Codex requires one-time approval for command hooks: review them in Codex `/hooks`, or pass `--trust-codex-hooks` to `install.sh` (which trusts only the four reviewed mailbox hook commands).

`<checkout>/plugins/orchestrate/uninstall.sh` removes the skill symlinks while leaving `~/.config/agent-executor`, `~/.cache/agent-executor`, and backups in `~/.local/share/agent-executor/backups` untouched.

## Inspect routes, models, and health

In the examples below, `<scripts>` stands for `<checkout>/plugins/orchestrate/skills/orchestrate/scripts` (or `~/.agents/skills/orchestrate/scripts` after running `install.sh`):

```sh
python3 <scripts>/run_agent.py doctor --check-messaging       # host, CLI versions, routes, skill links, and model-free MCP/hook check
python3 <scripts>/run_agent.py routes                         # effective role -> engine/model/effort table
python3 <scripts>/run_agent.py families                       # live resolution of family selectors (gpt:sol, gemini:fast, claude:best, …)
python3 <scripts>/run_agent.py models --engine codex --match sol  # query cached/live CLI model catalog (always filter with --match)
python3 <scripts>/run_agent.py quota                          # live quota per route from monitor, plus OpenRouter credit balance
node <checkout>/plugins/orchestrate/bin/ork.mjs --help        # rooms, stooges TUI, routes, quota, telemetry, and session naming
```

Routes are configured in `~/.config/agent-executor/config.json` (`run_agent.py init` writes a starter file) overlaid on [`routes.default.json`](skills/orchestrate/references/routes.default.json). Model selectors (`family` or `family:tier` from [`families.json`](skills/orchestrate/references/families.json)) resolve against the live catalog so model upgrades do not require config edits. For task archetypes and route tiers, see [`archetype-rationale.md`](skills/orchestrate/references/archetype-rationale.md).

## Ask one question (`consult`)

When an agent or developer needs a second opinion, root-cause investigation, or code review without handing off file ownership, `consult` runs the target model in its engine's no-write mode (Codex read-only sandbox, Claude Code with no tools or `Read,Grep,Glob` only, Antigravity plan mode, OpenCode standalone with write tools disabled):

```sh
# Focused question in an empty workspace (returns ANSWER and UNCERTAINTY)
python3 <scripts>/run_agent.py consult \
  --question 'Is unlinking a flock file after close safe on macOS?' \
  --attach src/lock.py --model gpt:sol

# Read-only repository review or investigation (returns FINDINGS, EVIDENCE, and UNCERTAINTY)
python3 <scripts>/run_agent.py consult --intent review \
  --read <repo> --diff origin/main \
  --question 'Any race conditions or missed edge cases?' --model claude:best

# Follow up in the same consultant session with context preserved
python3 <scripts>/run_agent.py consult \
  --question 'What if the file descriptor is inherited across fork+exec?' \
  --follow-up <result.json>
```

Details: [`skills/orchestrate/references/consult.md`](skills/orchestrate/references/consult.md).

## Delegate a bounded task (`run_agent.py`, `steer`, `probe`)

Implementation runs take a JSON `--task-spec` (or `--brief` with `--allow-path`/`--track-path`), acquire a worktree lease, verify that only allowed paths changed and Git refs/stash were not touched, and run repeatable `--verify-command` gates after the worker exits:

```sh
python3 <scripts>/run_agent.py \
  --cwd <repo> --task-spec /tmp/spec.json --route implementation \
  --verify-command 'yarn check' --verify-command 'yarn test' --expect-changes
```

- **Mid-run steering (`steer`):** Redirect a detached running job without killing its context or releasing its worktree lease. The runner interrupts the CLI with `SIGINT`, records the partial segment, and resumes the same session with the new instruction:

  ```sh
  python3 <scripts>/run_agent.py steer <event-id> --cwd <repo> --message @lead.md
  ```

- **Isolated patch testing (`probe`):** Copy selected files into a temporary `/tmp` overlay, apply a candidate patch, and run a test command without touching the checkout:

  ```sh
  python3 <scripts>/run_agent.py probe \
    --copy src/module.py --patch /tmp/candidate.patch \
    -- python3 -m unittest tests.test_module
  ```

Details: [`skills/orchestrate/references/runner.md`](skills/orchestrate/references/runner.md), [`skills/orchestrate/references/brief-template.md`](skills/orchestrate/references/brief-template.md), [`skills/orchestrate/references/completion.md`](skills/orchestrate/references/completion.md).

## Multi-agent coordination

### Peer mailbox (`peer-mailbox`)

Live sessions across harnesses (or across separate accounts of the same harness, such as `~/.claude` and `~/.claude-personal`) discover each other and exchange messages within a project through one authenticated loopback HTTP MCP daemon (`mailbox_daemon.py`), backed by `~/.cache/agent-executor/mailbox-v1/`:

```sh
python3 <scripts>/run_agent.py mailbox focus --session <id> --harness codex --text 'Working on API validation'
python3 <scripts>/run_agent.py mailbox peers
python3 <scripts>/run_agent.py mailbox send --to <peer-session-id> --body 'Question with evidence'
python3 <scripts>/run_agent.py mailbox wait --reply-to <message-id> --timeout 300
python3 <scripts>/mailbox_daemon.py status
```

For persistent headless Claude or Antigravity workers that should wake automatically when mail arrives, run `python3 <scripts>/run_agent.py listen --engine claude|agy --prompt 'Task'`. Details: [`skills/orchestrate/references/mailbox.md`](skills/orchestrate/references/mailbox.md).

### Coordinator, team authority, and deputy succession

`coordinator.py` manages multi-step task directories with invocation allowances, wall-clock deadlines, native-bridge guards, and captures evidence before review (`capture`, `verify-evidence`).

`authority.py` gives a multi-agent team one accountable lead with a compare-and-swap generation fence (`appoint`, `offer`, `takeover`, `accept`, `show`). `authority.py propose --session <id> --harness <harness>` inspects `monitor`'s quota pools (`accountId:poolId`) and account `billing` contexts (`work` or `personal`) to propose a deputy on an independent account and reserve both pools from worker routing. When appointed with `--standing-takeover STATUSES:SECONDS` (for example `blocked,credits:900`), the deputy can run `takeover --standing` once fresh `monitor` data shows the lead's pool is in one of those statuses, the lead has been inactive for that duration, no team task has a recently updated live run, no handover is already offered, and—on `credits`—the lead's process is gone.

Details: [`skills/orchestrate/references/coordinator.md`](skills/orchestrate/references/coordinator.md).

### Swarm rooms and Stooges TUI (`ork`)

`bin/ork.mjs` (`ork`) manages multi-agent rooms over a repository (`ork rooms`) and renders the live Stooges terminal dashboard (`ork stooges`) with preset teams (`scout-swarm`, `bug-court`, `patch-race`, `heavy-trifecta`):

```sh
node <checkout>/plugins/orchestrate/bin/ork.mjs stooges --preset bug-court
node <checkout>/plugins/orchestrate/bin/ork.mjs stooges --attach-room <room-id>
```

Details: [`skills/orchestrate/references/rooms.md`](skills/orchestrate/references/rooms.md). Full agent instructions: [`skills/orchestrate/SKILL.md`](skills/orchestrate/SKILL.md).

## Troubleshooting

- **`HTTP mailbox is running old code; use mailbox daemon restart after upgrade`:** Restart the background loopback daemon after pulling changes:

  ```sh
  python3 <scripts>/mailbox_daemon.py restart
  ```

- **Missing MCP registration or untrusted hooks:** Run `python3 <scripts>/run_agent.py doctor --check-messaging`. Repair with `python3 <scripts>/run_agent.py doctor --repair-messaging` (add `--trust-codex-hooks` after reviewing the four Codex mailbox hooks).
- **Duplicate `orchestrate` skill in Claude Code:** If `orchestrate@orkest` is installed as a Claude Code plugin and `~/.claude/skills/orchestrate` is also linked, run `<checkout>/plugins/orchestrate/uninstall.sh` followed by `<checkout>/plugins/orchestrate/install.sh --no-claude`.
- **Exit 15, 18, or 19 (quota or credits):** Exit 15 means the target quota pool is blocked; exit 18 means the pool is on paid credits at launch; exit 19 (`stopped_credits`) means a Codex run began spending credits mid-run and was stopped with partial work preserved. Check `run_agent.py quota` and pass `--allow-credits` only when the user explicitly authorizes credit spend.
- **Exit 17 (hosted model sent to OpenCode):** Hosted Claude, GPT, and Gemini models are refused on `--engine opencode`; use `--engine claude`, `--engine codex`, or `--engine agy`.
- **Exit 27 (worktree lease held):** Another writer (or reader when starting a writer) holds the repository's worktree lease. Wait for the active job (`run_agent.py events --cwd <repo>`) before launching another mutating run.
