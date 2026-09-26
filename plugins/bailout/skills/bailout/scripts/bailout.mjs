#!/usr/bin/env node
// Command line face of the bailout skill.
//
//   paths [session] [--cwd DIR] [--json]   where this session's handoff files go
//   status [session]                        usage, thresholds and stage
//   recover [session]                       write a recovery snapshot now
//   reset [session]                         forget this session's bailout state
//   simulate --session ID --percent N       drive the sampler with a fake reading
//
// `paths` works with no telemetry at all, so a manual bailout never depends on
// the status line being installed.

import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  bailoutHome,
  effectiveThresholds,
  fileExists,
  handoffDir,
  handoffPath,
  isStale,
  loadConfig,
  newState,
  nowSeconds,
  readJson,
  readState,
  sessionStatePath,
  writeJsonAtomic,
} from './lib.mjs';
import { writeRecovery } from './recovery.mjs';

const SCRIPTS_DIR = dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const positional = [];
  const flags = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg.startsWith('--')) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {
        flags[key] = true;
      } else {
        flags[key] = next;
        i += 1;
      }
    } else {
      positional.push(arg);
    }
  }
  return { positional, flags };
}

function resolveSession(positional, flags) {
  return positional[0] || flags.session || process.env.CLAUDE_SESSION_ID || 'manual';
}

function resolveState(sessionId, flags) {
  const state = readState(sessionId) || newState(sessionId);
  state.sessionId = sessionId;
  state.cwd = flags.cwd || state.cwd || process.cwd();
  return state;
}

function cmdPaths(positional, flags) {
  const sessionId = resolveSession(positional, flags);
  const state = resolveState(sessionId, flags);
  const result = {
    session: sessionId,
    cwd: state.cwd,
    directory: handoffDir(state.cwd, sessionId),
    checkpoint: handoffPath(state.cwd, sessionId, 'checkpoint'),
    handoff: handoffPath(state.cwd, sessionId, 'bailout'),
    recovery: handoffPath(state.cwd, sessionId, 'recovery'),
  };
  if (flags.json) {
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    return;
  }
  process.stdout.write(
    [
      `checkpoint: ${result.checkpoint}`,
      `handoff:    ${result.handoff}`,
      `recovery:   ${result.recovery}`,
      '',
    ].join('\n'),
  );
}

// The sampler swallows its own stderr, so a broken install shows up as usage
// that never updates rather than as an error. These checks make that visible.
function installProblems() {
  const receipt = readJson(join(bailoutHome(), 'install-receipt.json'));
  if (!receipt || !receipt.installedAt) return ['not installed (automatic triggering is off; /bailout still works)'];

  const problems = [];
  const samplerPath = receipt.sampler?.path;
  if (!samplerPath || !fileExists(samplerPath)) {
    problems.push(`status line script is missing: ${samplerPath || 'unknown'}`);
    return problems;
  }
  let body = '';
  try {
    body = readFileSync(samplerPath, 'utf8');
  } catch {
    problems.push(`status line script cannot be read: ${samplerPath}`);
    return problems;
  }
  const line = body.split('\n').find((l) => l.includes('hooks.mjs') && l.includes('statusline'));
  if (!line) {
    problems.push(`the sampler block is no longer in ${samplerPath} (something rewrote it)`);
    return problems;
  }
  for (const quoted of line.match(/"([^"]+)"/g) || []) {
    const path = quoted.slice(1, -1);
    if (path.startsWith('/') && !fileExists(path)) {
      problems.push(`the sampler references a path that no longer exists: ${path}`);
    }
  }
  return problems;
}

function cmdStatus(positional, flags) {
  const config = loadConfig();
  const sessionId = resolveSession(positional, flags);
  const state = readState(sessionId);
  const problems = installProblems();
  const lines = [`session:    ${sessionId}`];
  lines.push(`install:    ${problems.length ? 'PROBLEM' : 'ok'}`);
  for (const problem of problems) lines.push(`  - ${problem}`);
  if (problems.length) lines.push('  fix with: node ~/.claude/skills/bailout/scripts/install.mjs');

  if (!state) {
    lines.push(
      'usage:      unknown (no sample recorded for this session)',
      '',
      'No status line sample has reached the bailout state file. Either the status',
      'line hook is not installed, or this session has not had an API response yet.',
      'Manual bailout still works: run the /bailout skill.',
      '',
    );
    process.stdout.write(lines.join('\n'));
    return;
  }

  const thresholds = state.thresholds || effectiveThresholds(config, state.modelId);
  const stale = isStale(state, config);
  const used = state.usage?.usedPercentage;

  // Distinguish "the sampler never ran" from "it ran but the payload carried no
  // reading". Only the first means the status line is not wired into this
  // session, which is the normal state until Claude Code is restarted after
  // installing.
  if (!state.usage && !state.usageUnavailableAt) {
    lines.push(
      'sampler:    has not run in this session',
      '  The status line feeds the sampler, and a session loads it at startup.',
      '  Restart Claude Code if this session predates the install.',
    );
  } else if (!state.usage) {
    lines.push('sampler:    running, but the payload carried no five-hour reading');
  }
  lines.push(`model:      ${state.modelDisplay || state.modelId || 'unknown'}`);
  lines.push(
    `usage:      ${
      typeof used === 'number' && !stale
        ? `${used}% of the five-hour allowance consumed`
        : typeof used === 'number'
          ? `unknown (last sample ${used}% is stale, older than ${config.staleAfterSeconds}s)`
          : 'unknown (no five-hour reading; rate_limits needs a Pro or Max subscription and one API response)'
    }`,
  );
  lines.push(
    `thresholds: checkpoint ${thresholds.checkpoint}%, bailout ${thresholds.bailout}% (model margin ${thresholds.margin})`,
  );
  lines.push(`stage:      ${state.stage}`);
  if (state.window?.resetsAt) {
    lines.push(`window:     resets at ${new Date(state.window.resetsAt * 1000).toISOString()}`);
  }
  for (const [kind, path] of Object.entries(state.handoffs || {})) {
    lines.push(`${kind}: ${path}${fileExists(path) ? '' : ' (missing)'}`);
  }
  lines.push('');
  process.stdout.write(lines.join('\n'));
}

function cmdRecover(positional, flags) {
  const config = loadConfig();
  const sessionId = resolveSession(positional, flags);
  const state = resolveState(sessionId, flags);
  const path = writeRecovery(state, config, 'recovery was requested explicitly');
  state.handoffs = { ...state.handoffs, recovery: path };
  writeJsonAtomic(sessionStatePath(sessionId), state);
  process.stdout.write(`${path}\n`);
}

function cmdReset(positional, flags) {
  const sessionId = resolveSession(positional, flags);
  const fresh = newState(sessionId);
  fresh.cwd = flags.cwd || process.cwd();
  writeJsonAtomic(sessionStatePath(sessionId), fresh);
  process.stdout.write(`reset ${sessionStatePath(sessionId)}\n`);
}

// Drives the real sampler with a synthetic reading so thresholds can be
// exercised without spending any of the actual allowance. Point BAILOUT_HOME at
// a scratch directory before using it.
function cmdSimulate(positional, flags) {
  const sessionId = resolveSession(positional, flags);
  const percent = Number(flags.percent);
  if (Number.isNaN(percent)) {
    process.stderr.write('simulate needs --percent <number>\n');
    process.exitCode = 1;
    return;
  }
  const payload = {
    session_id: sessionId,
    cwd: flags.cwd || process.cwd(),
    model: { id: flags.model || 'claude-opus-5', display_name: flags.model || 'Opus' },
    rate_limits: {
      five_hour: {
        used_percentage: percent,
        resets_at: Number(flags.resets) || nowSeconds() + 3600,
      },
    },
  };
  spawnSync(process.execPath, [join(SCRIPTS_DIR, 'hooks.mjs'), 'statusline'], {
    input: JSON.stringify(payload),
    stdio: ['pipe', 'inherit', 'inherit'],
  });
  cmdStatus([sessionId], flags);
}

const COMMANDS = {
  paths: cmdPaths,
  status: cmdStatus,
  recover: cmdRecover,
  reset: cmdReset,
  simulate: cmdSimulate,
};

const { positional, flags } = parseArgs(process.argv.slice(2));
const command = COMMANDS[positional.shift()];
if (!command) {
  process.stdout.write(
    'usage: bailout.mjs <paths|status|recover|reset|simulate> [session] [--cwd DIR] [--json]\n',
  );
  process.exit(1);
}
command(positional, flags);
