#!/usr/bin/env node
// Hook driver for the bailout skill. One file, four modes:
//
//   statusline   fed the status line JSON, samples rate_limits.five_hour and
//                decides whether a checkpoint or bailout is due. Prints nothing.
//   gate         PostToolUse. Delivers a pending decision as additionalContext,
//                which reaches Claude mid-task without a new user prompt.
//   stop         Stop. Asks again, a bounded number of times, when a bailout was
//                requested and the handoff is still missing.
//   session-end  SessionEnd. Writes a labelled recovery snapshot if the session
//                ended with a bailout outstanding. Makes no model request.
//
// Every mode fails open: a broken bailout install must never break a tool call.

import { mkdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  effectiveThresholds,
  fileExists,
  handoffDir,
  handoffPath,
  loadConfig,
  newState,
  nowSeconds,
  parseJsonSafe,
  readFiveHour,
  readState,
  readStdin,
  writeState,
} from './lib.mjs';
import { writeRecovery } from './recovery.mjs';

const SKILL_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
const GUIDE = join(SKILL_DIR, 'SKILL.md');

const BAILOUT_STAGES = new Set(['bailout-armed', 'bailout-delivered']);

function fileNewerThan(path, epochSeconds) {
  try {
    return statSync(path).mtimeMs / 1000 >= epochSeconds - 2;
  } catch {
    return false;
  }
}

// Which handoffs exist is derived from disk every time, never remembered, so a
// file that is deleted or left over from an earlier quota window cannot keep
// counting. A handoff only answers the request it was asked for: it has to be
// at least as new as the moment that kind armed.
function reconcile(state) {
  if (!state.cwd) return state;
  const armed = state.armed || {};
  const handoffs = state.handoffs?.recovery ? { recovery: state.handoffs.recovery } : {};
  for (const kind of ['checkpoint', 'bailout']) {
    const armedAt = armed[kind];
    if (!armedAt) continue;
    const path = handoffPath(state.cwd, state.sessionId, kind);
    if (fileExists(path) && fileNewerThan(path, armedAt)) handoffs[kind] = path;
  }
  state.handoffs = handoffs;
  if (handoffs.bailout && BAILOUT_STAGES.has(state.stage)) {
    state.stage = 'bailout-done';
  } else if (handoffs.checkpoint && state.stage === 'checkpoint-delivered') {
    state.stage = 'checkpoint-done';
  }
  return state;
}

function arm(state, kind, used, thresholds, now) {
  // Create the directory now, so the absolute path handed to Claude is one it
  // can write to immediately and the user can list before the file exists.
  try {
    mkdirSync(handoffDir(state.cwd, state.sessionId), { recursive: true });
  } catch {
    // A handoff written straight to the path still creates it.
  }
  state.stage = `${kind}-armed`;
  state.armed = { ...(state.armed || {}), [kind]: now };
  state.pending = {
    kind,
    armedAt: now,
    usedPercentage: used,
    threshold: kind === 'bailout' ? thresholds.bailout : thresholds.checkpoint,
  };
  state.stopBlocks = 0;
}

function evaluate(state, config, thresholds, used, now) {
  if (used >= thresholds.bailout) {
    if (!state.stage.startsWith('bailout')) arm(state, 'bailout', used, thresholds, now);
    return;
  }
  if (config.checkpointEnabled && used >= thresholds.checkpoint && state.stage === 'idle') {
    arm(state, 'checkpoint', used, thresholds, now);
  }
}

function modelLabel(state) {
  return state.modelDisplay || state.modelId || 'the current model';
}

function sampleAge(state, now) {
  const sampledAt = state?.usage?.sampledAt;
  if (typeof sampledAt !== 'number') return 'age unknown';
  const age = Math.max(0, now - sampledAt);
  return age < 90 ? `sampled ${age}s ago` : `sampled ${Math.round(age / 60)} min ago`;
}

// Written as statements of environment state and local convention. Text framed
// as out-of-band system commands trips Claude's prompt-injection defences and
// gets surfaced to the user instead of acted on.
function instructionText(state, config, kind, now) {
  const thresholds = state.thresholds || effectiveThresholds(config, state.modelId);
  const used = state.usage?.usedPercentage;
  const marginNote = thresholds.margin
    ? `, pulled ${thresholds.margin} point${thresholds.margin === 1 ? '' : 's'} earlier for ${modelLabel(state)}`
    : '';
  const head =
    `The five-hour Claude subscription allowance on this account is ${used}% consumed ` +
    `(${sampleAge(state, now)}). The bailout threshold for this session is ` +
    `${kind === 'bailout' ? thresholds.bailout : thresholds.checkpoint}%${marginNote}.`;

  if (kind === 'checkpoint') {
    const path = handoffPath(state.cwd, state.sessionId, 'checkpoint');
    return [
      head,
      '',
      'The bailout convention on this machine is to save one short checkpoint at this point, so the context of the current task survives if the allowance runs out abruptly, and then to carry on with ordinary work.',
      '',
      `Checkpoint file: ${path}`,
      `Writing guide: ${GUIDE}`,
      '',
      'A checkpoint is short: what the user wants, where the work stopped, and the next action. It is requested once per five-hour window, not on every turn.',
    ].join('\n');
  }

  const path = handoffPath(state.cwd, state.sessionId, 'bailout');
  const checkpoint = state.handoffs.checkpoint;
  return [
    head,
    '',
    'Remaining capacity is not guaranteed: one large operation, or other activity on the account, can consume it before another response completes.',
    '',
    'The bailout convention on this machine at this point is that no new ordinary work is started, the handoff file below is written or finalized, and the turn ends reporting its absolute path.',
    '',
    `Handoff file: ${path}`,
    `Writing guide and required sections: ${GUIDE}`,
    checkpoint ? `Earlier checkpoint to build on: ${checkpoint}` : 'No earlier checkpoint exists for this session.',
    '',
    'Reads needed to make the handoff accurate are part of finishing it. The reader is an agent with none of this conversation: Codex, Gemini, or a later Claude session.',
  ].join('\n');
}

async function readHookInput() {
  return parseJsonSafe(await readStdin()) || {};
}

function loadSession(input) {
  const sessionId = input.session_id;
  if (!sessionId) return null;
  const state = readState(sessionId) || newState(sessionId);
  state.sessionId = sessionId;
  state.cwd = input.cwd || input.workspace?.current_dir || state.cwd || process.cwd();
  return state;
}

async function modeStatusline(config) {
  const input = await readHookInput();
  const state = loadSession(input);
  if (!state) return;

  const now = nowSeconds();
  if (input.transcript_path) state.transcriptPath = input.transcript_path;
  if (input.model?.id) state.modelId = input.model.id;
  if (input.model?.display_name) state.modelDisplay = input.model.display_name;
  if (input.version) state.ccVersion = input.version;
  if (input.session_name) state.sessionName = input.session_name;
  if (input.workspace?.current_dir) state.cwd = input.workspace.current_dir;
  if (typeof input.context_window?.used_percentage === 'number') {
    state.contextUsedPercentage = input.context_window.used_percentage;
  }

  const five = readFiveHour(input);
  if (five) {
    // A changed resets_at is the only evidence that the quota window rolled
    // over. A new session is not evidence, so stage never resets on session id.
    const previousResetsAt = state.window ? state.window.resetsAt : undefined;
    if (previousResetsAt !== five.resetsAt) {
      state.window = { resetsAt: five.resetsAt };
      state.stage = 'idle';
      state.pending = null;
      state.stopBlocks = 0;
      state.armed = {};
      state.handoffs = {};
    }
    state.usage = {
      usedPercentage: five.usedPercentage,
      resetsAt: five.resetsAt,
      sampledAt: now,
    };
    const thresholds = effectiveThresholds(config, state.modelId);
    state.thresholds = thresholds;
    reconcile(state);
    evaluate(state, config, thresholds, five.usedPercentage, now);
  } else {
    // Missing rate_limits is unknown, never zero: leave the stage alone.
    state.usageUnavailableAt = now;
  }
  writeState(state);
}

async function modeGate(config) {
  const input = await readHookInput();
  // agent_id is present only inside a subagent. The handoff belongs to the main
  // session, which picks the instruction up on its own next tool call.
  if (input.agent_id) return;
  const state = loadSession(input);
  if (!state) return;
  reconcile(state);
  const pending = state.pending;
  if (!pending) {
    writeState(state);
    return;
  }
  const now = nowSeconds();
  const text = instructionText(state, config, pending.kind, now);
  state.stage = `${pending.kind}-delivered`;
  state.pending = null;
  state.deliveredAt = now;
  writeState(state);
  process.stdout.write(
    `${JSON.stringify({
      hookSpecificOutput: { hookEventName: 'PostToolUse', additionalContext: text },
    })}\n`,
  );
}

async function modeStop(config) {
  const input = await readHookInput();
  const state = loadSession(input);
  if (!state) return;
  reconcile(state);

  if (!BAILOUT_STAGES.has(state.stage)) {
    writeState(state);
    return;
  }
  // stop_hook_active means Claude Code is already continuing because of a stop
  // hook. Combined with the block budget this keeps the loop finite.
  if (state.stopBlocks >= config.maxStopBlocks) {
    writeState(state);
    return;
  }
  const now = nowSeconds();
  state.stopBlocks += 1;
  const pendingKind = state.pending?.kind;
  state.pending = null;
  if (pendingKind) state.stage = 'bailout-delivered';
  writeState(state);
  const reason = `${instructionText(state, config, 'bailout', now)}\n\nThe handoff file does not exist yet. Attempt ${state.stopBlocks} of ${config.maxStopBlocks}.`;
  process.stdout.write(`${JSON.stringify({ decision: 'block', reason })}\n`);
}

async function modeSessionEnd(config) {
  const input = await readHookInput();
  const state = loadSession(input);
  if (!state) return;
  reconcile(state);
  if (BAILOUT_STAGES.has(state.stage) && !state.handoffs.bailout) {
    const path = writeRecovery(state, config, 'session ended with a bailout outstanding');
    state.handoffs.recovery = path;
    state.stage = 'recovered';
  }
  writeState(state);
}

const MODES = {
  statusline: modeStatusline,
  gate: modeGate,
  stop: modeStop,
  'session-end': modeSessionEnd,
};

async function main() {
  const mode = process.argv[2];
  const run = MODES[mode];
  if (!run) {
    process.stderr.write(`bailout hooks: unknown mode ${mode}\n`);
    process.exit(0);
  }
  const config = loadConfig();
  if (!config.enabled) return;
  await run(config);
}

main().catch((error) => {
  // Fail open. A hook error must not block the tool call that triggered it.
  process.stderr.write(`bailout hooks: ${error && error.message}\n`);
  process.exit(0);
});
