#!/usr/bin/env node
// Hook driver for the bailout skill. One file, four modes:
//
//   statusline   fed the status line JSON, samples rate_limits.five_hour and
//                decides whether a checkpoint or bailout is due. Prints nothing.
//   gate         PostToolUse. Delivers a pending decision as additionalContext,
//                which reaches Claude mid-task without a new user prompt.
//   stop         Stop. Asks again, a bounded number of times, when a bailout was
//                requested and the handoff is still missing. Never ends the
//                session: Claude Code's own usage-limit pause and automatic
//                continue have to see the real limit to resume it.
//   session-end  SessionEnd. Writes a labelled recovery snapshot if the session
//                ended with a bailout outstanding. Makes no model request.
//
// Every mode fails open: a broken bailout install must never break a tool call.

import { mkdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  disarmSession,
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
  sessionArming,
  writeState,
} from "./lib.mjs";
import { writeRecovery } from "./recovery.mjs";

const SKILL_DIR = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const GUIDE = path.join(SKILL_DIR, "SKILL.md");

const BAILOUT_STAGES = new Set(["bailout-armed", "bailout-delivered"]);

// A window is over once its reset time has passed, whether or not the status
// line has delivered a reading from the next one. Claude Code drops the
// five-hour reading for a while after a reset, so waiting for that reading would
// carry a bailout armed in the old window into the session it resumes.
function hasWindowEnded(resetsAt, now) {
  return typeof resetsAt === "number" && now >= resetsAt;
}

function startWindow(state, resetsAt) {
  state.window = { resetsAt };
  state.stage = "idle";
  state.pending = null;
  state.stopBlocks = 0;
  state.armed = {};
  state.handoffs = {};
  delete state.announced;
}

function rollOver(state, now) {
  if (hasWindowEnded(state.window?.resetsAt, now)) startWindow(state, null);
}

function fileNewerThan(filePath, epochSeconds) {
  try {
    return statSync(filePath).mtimeMs / 1000 >= epochSeconds - 2;
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
  for (const kind of ["checkpoint", "bailout"]) {
    const armedAt = armed[kind];
    if (!armedAt) continue;
    const filePath = handoffPath(state.cwd, state.sessionId, kind);
    if (fileExists(filePath) && fileNewerThan(filePath, armedAt)) handoffs[kind] = filePath;
  }
  state.handoffs = handoffs;
  if (handoffs.bailout && BAILOUT_STAGES.has(state.stage)) {
    state.stage = "bailout-done";
  } else if (handoffs.checkpoint && state.stage === "checkpoint-delivered") {
    state.stage = "checkpoint-done";
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
  state.armed = { ...state.armed, [kind]: now };
  state.pending = {
    kind,
    armedAt: now,
    usedPercentage: used,
    threshold: kind === "bailout" ? thresholds.bailout : thresholds.checkpoint,
  };
  state.stopBlocks = 0;
}

function evaluate(state, config, thresholds, used, now) {
  if (used >= thresholds.bailout) {
    if (!state.stage.startsWith("bailout")) arm(state, "bailout", used, thresholds, now);
    return;
  }
  if (config.checkpointEnabled && used >= thresholds.checkpoint && state.stage === "idle") {
    arm(state, "checkpoint", used, thresholds, now);
  }
}

function modelLabel(state) {
  return state.modelDisplay || state.modelId || "the current model";
}

function sampleAge(state, now) {
  const sampledAt = state?.usage?.sampledAt;
  if (typeof sampledAt !== "number") return "age unknown";
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
    ? `, pulled ${thresholds.margin} point${thresholds.margin === 1 ? "" : "s"} earlier for ${modelLabel(state)}`
    : "";
  const head =
    `The five-hour Claude subscription allowance on this account is ${used}% consumed ` +
    `(${sampleAge(state, now)}). The bailout threshold for this session is ` +
    `${kind === "bailout" ? thresholds.bailout : thresholds.checkpoint}%${marginNote}.`;

  if (kind === "checkpoint") {
    const filePath = handoffPath(state.cwd, state.sessionId, "checkpoint");
    return [
      head,
      "",
      "The bailout convention on this machine is to save one short checkpoint at this point, so the context of the current task survives if the allowance runs out abruptly, and then to carry on with ordinary work.",
      "",
      `Checkpoint file: ${filePath}`,
      `Writing guide: ${GUIDE}`,
      "",
      "A checkpoint is short: what the user wants, where the work stopped, and the next action. It is requested once per five-hour window, not on every turn.",
    ].join("\n");
  }

  const filePath = handoffPath(state.cwd, state.sessionId, "bailout");
  const checkpoint = state.handoffs.checkpoint;
  return [
    head,
    "",
    "Remaining capacity is not guaranteed: one large operation, or other activity on the account, can consume it before another response completes.",
    "",
    "The bailout convention on this machine at this point is that the handoff file below is written or finalized before any other work, its absolute path is reported in one line, and then the current task carries on. If the allowance runs out mid-turn, Claude Code pauses the session and continues it by itself once the window resets; the handoff records where the work stood and what was next, for when it does not.",
    "",
    `Handoff file: ${filePath}`,
    `Writing guide and required sections: ${GUIDE}`,
    checkpoint
      ? `Earlier checkpoint to build on: ${checkpoint}`
      : "No earlier checkpoint exists for this session.",
    "",
    "Reads needed to make the handoff accurate are part of finishing it. The reader is an agent with none of this conversation: Codex, Gemini, or a later Claude session.",
  ].join("\n");
}

async function readHookInput() {
  return parseJsonSafe(await readStdin()) || {};
}

function loadSession(input) {
  const sessionId = input.session_id;
  if (typeof sessionId !== "string" || !sessionId || input.agent_id) return null;
  const cwd = input.cwd || input.workspace?.current_dir;
  if (!sessionArming(sessionId, cwd)) return null;
  const state = readState(sessionId) || newState(sessionId);
  state.sessionId = sessionId;
  state.cwd = cwd;
  return state;
}

async function modeStatusline(config) {
  const input = await readHookInput();
  const state = loadSession(input);
  if (!state) return;

  const now = nowSeconds();
  rollOver(state, now);
  if (input.transcript_path) state.transcriptPath = input.transcript_path;
  if (input.model?.id) state.modelId = input.model.id;
  if (input.model?.display_name) state.modelDisplay = input.model.display_name;
  if (input.version) state.ccVersion = input.version;
  if (input.session_name) state.sessionName = input.session_name;
  if (input.workspace?.current_dir) state.cwd = input.workspace.current_dir;
  if (typeof input.context_window?.used_percentage === "number") {
    state.contextUsedPercentage = input.context_window.used_percentage;
  }

  const five = readFiveHour(input);
  // A reading whose window has already reset describes the previous window.
  if (five && !hasWindowEnded(five.resetsAt, now)) {
    // A changed resets_at, or a reset time that has passed, is the only evidence
    // that the quota window rolled over. A new session is not evidence, so stage
    // never resets on session id.
    if (state.window?.resetsAt !== five.resetsAt) startWindow(state, five.resetsAt);
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
    // Missing or outdated rate_limits is unknown, never zero: leave the stage alone.
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
  const now = nowSeconds();
  rollOver(state, now);
  reconcile(state);
  const pending = state.pending;
  if (!pending) {
    writeState(state);
    return;
  }
  const text = instructionText(state, config, pending.kind, now);
  state.stage = `${pending.kind}-delivered`;
  state.pending = null;
  state.deliveredAt = now;
  writeState(state);
  process.stdout.write(
    `${JSON.stringify({
      hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext: text },
    })}\n`,
  );
}

// A notice for the user, shown once per window and outcome. It never stops the
// turn. Returns the hook output to print after the state is written.
function noticeOnce(state, key, systemMessage) {
  if (state.announced === key) return null;
  state.announced = key;
  return { systemMessage };
}

function emit(output) {
  if (output) process.stdout.write(`${JSON.stringify(output)}\n`);
}

async function modeStop(config) {
  const input = await readHookInput();
  const state = loadSession(input);
  if (!state) return;
  const now = nowSeconds();
  rollOver(state, now);
  reconcile(state);

  // Once the handoff exists the hook steps aside, and no longer overrides a
  // /goal or any other Stop hook that keeps the work going. The session runs
  // into the real usage limit, where Claude Code pauses it and continues it by
  // itself when the window resets. Ending the session here instead, with
  // `continue: false`, would stop it short of that limit, and nothing would
  // resume it.
  if (state.stage === "bailout-done") {
    const handoff = state.handoffs.bailout;
    const notice = noticeOnce(
      state,
      handoff,
      `Usage bailout: handoff saved to ${handoff}. Work continues; if the allowance runs out, Claude Code resumes the session after the window resets.`,
    );
    writeState(state);
    emit(notice);
    return;
  }
  if (!BAILOUT_STAGES.has(state.stage)) {
    writeState(state);
    return;
  }
  // stop_hook_active means Claude Code is already continuing because of a stop
  // hook. Combined with the block budget this keeps the loop finite. Past the
  // budget the hook stops asking but still does not end the session.
  if (state.stopBlocks >= config.maxStopBlocks) {
    const notice = noticeOnce(
      state,
      "unanswered",
      "Usage bailout: the handoff was requested but not written; the session-end hook will save a recovery snapshot.",
    );
    writeState(state);
    emit(notice);
    return;
  }
  state.stopBlocks += 1;
  const pendingKind = state.pending?.kind;
  state.pending = null;
  if (pendingKind) state.stage = "bailout-delivered";
  writeState(state);
  const reason = `${instructionText(state, config, "bailout", now)}\n\nThe handoff file does not exist yet. Attempt ${state.stopBlocks} of ${config.maxStopBlocks}.`;
  process.stdout.write(`${JSON.stringify({ decision: "block", reason })}\n`);
}

async function modeSessionEnd(config) {
  const input = await readHookInput();
  if (typeof input.session_id !== "string" || !input.session_id || input.agent_id) return;
  try {
    const state = loadSession(input);
    if (!state || config.enabled !== true) return;
    rollOver(state, nowSeconds());
    reconcile(state);
    if (BAILOUT_STAGES.has(state.stage) && !state.handoffs.bailout) {
      const filePath = writeRecovery(state, config, "session ended with a bailout outstanding");
      state.handoffs.recovery = filePath;
      state.stage = "recovered";
    }
    writeState(state);
  } finally {
    disarmSession(input.session_id);
  }
}

const MODES = {
  statusline: modeStatusline,
  gate: modeGate,
  stop: modeStop,
  "session-end": modeSessionEnd,
};

async function main() {
  const mode = process.argv[2];
  const run = MODES[mode];
  if (!run) {
    process.stderr.write(`bailout hooks: unknown mode ${mode}\n`);
    process.exit(0);
  }
  const config = loadConfig();
  if (config.enabled !== true && mode !== "session-end") return;
  await run(config);
}

try {
  await main();
} catch (error) {
  // Fail open. A hook error must not block the tool call that triggered it.
  process.stderr.write(`bailout hooks: ${error && error.message}\n`);
  process.exit(0);
}
