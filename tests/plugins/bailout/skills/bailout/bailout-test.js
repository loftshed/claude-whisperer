// Fixture-driven tests for the bailout skill. Every test runs against a fresh
// temporary BAILOUT_HOME and fake settings files, so none of this touches the
// real configuration and none of it spends any actual allowance.

import { spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test as baseTest, expect } from "vitest";

// Tests mirror the repository layout under tests/, so the skill under test sits at the
// same relative path below the repository root.
const TESTS_DIR = path.dirname(fileURLToPath(import.meta.url));
const SKILL_DIR = path.join(
  TESTS_DIR,
  "..",
  "..",
  "..",
  "..",
  "..",
  "plugins",
  "bailout",
  "skills",
  "bailout",
);
const HOOKS = path.join(SKILL_DIR, "scripts", "hooks.mjs");
const CLI = path.join(SKILL_DIR, "scripts", "bailout.mjs");
const INSTALL = path.join(SKILL_DIR, "scripts", "install.mjs");

function assertIncludes(haystack, needle, message) {
  expect(String(haystack), message).toContain(needle);
}

function assertNotIncludes(haystack, needle, message) {
  expect(String(haystack), message).not.toContain(needle);
}

// ── harness ────────────────────────────────────────────────────────────────

function makeContext() {
  const root = mkdtempSync(path.join(tmpdir(), "bailout-test-"));
  const home = path.join(root, "home");
  const project = path.join(root, "project");
  mkdirSync(home, { recursive: true });
  mkdirSync(project, { recursive: true });
  return {
    root,
    home,
    project,
    env: {
      ...process.env,
      BAILOUT_HOME: home,
      BAILOUT_CONFIG: path.join(home, "config.json"),
      CLAUDE_SESSION_ID: "",
    },
  };
}

// Each test gets its own temporary home and project, removed afterwards.
function contextFixture(isArmed) {
  return baseTest.extend({
    // eslint-disable-next-line no-empty-pattern -- Vitest fixtures must destructure their context.
    ctx: async ({}, use) => {
      const ctx = makeContext();
      if (isArmed) arm(ctx);
      await use(ctx);
      rmSync(ctx.root, { recursive: true, force: true });
    },
  });
}
const test = contextFixture(true);
const inactiveTest = contextFixture(false);

function run(ctx, script, args, input) {
  const result = spawnSync(process.execPath, [script, ...args], {
    input: input === undefined ? "" : input,
    encoding: "utf8",
    env: ctx.env,
  });
  return { stdout: result.stdout || "", stderr: result.stderr || "", status: result.status };
}

function statusline(ctx, payload) {
  return run(ctx, HOOKS, ["statusline"], JSON.stringify(payload));
}

function gate(ctx, payload) {
  return run(ctx, HOOKS, ["gate"], JSON.stringify(payload));
}

function stopHook(ctx, payload) {
  return run(ctx, HOOKS, ["stop"], JSON.stringify(payload));
}

function sessionEnd(ctx, payload) {
  return run(ctx, HOOKS, ["session-end"], JSON.stringify(payload));
}

function cli(ctx, args) {
  return run(ctx, CLI, args);
}

function arm(ctx, session = "s1", cwd = ctx.project) {
  const result = cli(ctx, ["arm", session, "--cwd", cwd]);
  expect(result.status, result.stderr).toBe(0);
  return result;
}

function sample(
  ctx,
  { session = "s1", percent, model = "claude-opus-5", resets = 2_000_000_000, cwd },
) {
  return statusline(ctx, {
    session_id: session,
    cwd: cwd || ctx.project,
    transcript_path: path.join(ctx.root, "transcript.jsonl"),
    version: "2.1.276",
    model: { id: model, display_name: model.includes("opus") ? "Opus" : "Model" },
    rate_limits: { five_hour: { used_percentage: percent, resets_at: resets } },
  });
}

function hookPayload(ctx, session = "s1") {
  return { session_id: session, cwd: ctx.project, hook_event_name: "PostToolUse" };
}

function state(ctx, session = "s1") {
  return JSON.parse(readFileSync(path.join(ctx.home, "sessions", `${session}.json`), "utf8"));
}

function paths(ctx, session = "s1") {
  const out = cli(ctx, ["paths", session, "--cwd", ctx.project, "--json"]).stdout;
  return JSON.parse(out);
}

function writeConfig(ctx, config) {
  mkdirSync(ctx.home, { recursive: true });
  writeFileSync(path.join(ctx.home, "config.json"), JSON.stringify(config, null, 2));
}

function additionalContext(result) {
  if (!result.stdout.trim()) return null;
  return JSON.parse(result.stdout).hookSpecificOutput?.additionalContext ?? null;
}

// ── threshold behaviour ────────────────────────────────────────────────────

test("below every threshold nothing arms and the gate stays silent", ({ ctx }) => {
  sample(ctx, { percent: 40 });
  expect(state(ctx).stage === "idle", "stage should stay idle").toBeTruthy();
  const result = gate(ctx, hookPayload(ctx));
  expect(result.stdout.trim() === "", "gate should print nothing below thresholds").toBeTruthy();
  expect(result.status === 0, "gate should exit 0").toBeTruthy();
});

test("checkpoint threshold arms once and the gate delivers it once", ({ ctx }) => {
  // Opus margin is 3, so the checkpoint threshold is 87.
  sample(ctx, { percent: 88 });
  expect(
    state(ctx).stage === "checkpoint-armed",
    `expected checkpoint-armed, got ${state(ctx).stage}`,
  ).toBeTruthy();

  const first = additionalContext(gate(ctx, hookPayload(ctx)));
  assertIncludes(first, "checkpoint.md", "checkpoint instruction should name the checkpoint file");
  assertIncludes(first, "88%", "instruction should state the observed percentage");
  expect(
    state(ctx).stage === "checkpoint-delivered",
    "stage should advance after delivery",
  ).toBeTruthy();

  const second = gate(ctx, hookPayload(ctx));
  expect(second.stdout.trim() === "", "the checkpoint must not be requested twice").toBeTruthy();
});

test("repeated samples at the same level do not re-arm", ({ ctx }) => {
  sample(ctx, { percent: 88 });
  gate(ctx, hookPayload(ctx));
  for (let i = 0; i < 5; i += 1) sample(ctx, { percent: 88 + i * 0.1 });
  const result = gate(ctx, hookPayload(ctx));
  expect(
    result.stdout.trim() === "",
    "repeated samples must not produce repeated requests",
  ).toBeTruthy();
});

test("bailout threshold arms and overrides an undelivered checkpoint", ({ ctx }) => {
  sample(ctx, { percent: 88 });
  sample(ctx, { percent: 93 });
  expect(
    state(ctx).stage === "bailout-armed",
    `expected bailout-armed, got ${state(ctx).stage}`,
  ).toBeTruthy();
  const text = additionalContext(gate(ctx, hookPayload(ctx)));
  assertIncludes(text, "handoff.md", "bailout instruction should name the handoff file");
  assertIncludes(
    text,
    "written or finalized before any other work",
    "bailout instruction should put the handoff first",
  );
  assertIncludes(
    text,
    "the current task carries on",
    "bailout instruction should keep the work going into the real limit",
  );
  assertNotIncludes(text, "no new ordinary work", "bailout instruction must not stop the work");
});

test("a session that starts already above the bailout threshold arms immediately", ({ ctx }) => {
  sample(ctx, { percent: 97 });
  expect(
    state(ctx).stage === "bailout-armed",
    `expected bailout-armed, got ${state(ctx).stage}`,
  ).toBeTruthy();
  const text = additionalContext(gate(ctx, hookPayload(ctx)));
  assertIncludes(text, "handoff.md", "should go straight to the final handoff");
  assertNotIncludes(text, "checkpoint.md", "should not ask for a checkpoint first");
});

test("the model margin moves the threshold and heavier models bail out earlier", ({ ctx }) => {
  for (const session of ["opus", "sonnet", "haiku"]) arm(ctx, session);
  sample(ctx, { session: "opus", percent: 92.5, model: "claude-opus-5" });
  expect(
    state(ctx, "opus").stage === "bailout-armed",
    "opus should bail out at 92.5 (threshold 92)",
  ).toBeTruthy();

  sample(ctx, { session: "sonnet", percent: 92.5, model: "claude-sonnet-5" });
  expect(
    state(ctx, "sonnet").stage === "checkpoint-armed",
    "sonnet threshold is 94, so 92.5 is only a checkpoint",
  ).toBeTruthy();

  sample(ctx, { session: "haiku", percent: 94.5, model: "claude-haiku-4-5" });
  expect(
    state(ctx, "haiku").stage === "checkpoint-armed",
    "haiku keeps the unadjusted 95 threshold",
  ).toBeTruthy();

  sample(ctx, { session: "haiku", percent: 95.5, model: "claude-haiku-4-5" });
  expect(state(ctx, "haiku").stage === "bailout-armed", "haiku bails out at 95").toBeTruthy();
});

test("the early checkpoint can be disabled without disabling the bailout", ({ ctx }) => {
  writeConfig(ctx, { checkpointEnabled: false });
  sample(ctx, { percent: 88 });
  expect(state(ctx).stage === "idle", "no checkpoint should arm when it is disabled").toBeTruthy();
  sample(ctx, { percent: 93 });
  expect(state(ctx).stage === "bailout-armed", "the bailout should still arm").toBeTruthy();
});

test("thresholds are configurable", ({ ctx }) => {
  writeConfig(ctx, {
    bailoutPercent: 60,
    checkpointPercent: 50,
    modelMarginPercent: { default: 0, opus: 0 },
  });
  sample(ctx, { percent: 55 });
  expect(
    state(ctx).stage === "checkpoint-armed",
    "custom checkpoint threshold should apply",
  ).toBeTruthy();
  sample(ctx, { percent: 61 });
  expect(
    state(ctx).stage === "bailout-armed",
    "custom bailout threshold should apply",
  ).toBeTruthy();
});

// ── bad, missing and stale data ────────────────────────────────────────────

test("missing rate_limits is unknown, not zero", ({ ctx }) => {
  statusline(ctx, { session_id: "s1", cwd: ctx.project, model: { id: "claude-opus-5" } });
  const s = state(ctx);
  expect(s.stage === "idle", "stage should stay idle without a reading").toBeTruthy();
  expect(!s.usage, "no usage should be recorded").toBeTruthy();
  assertIncludes(cli(ctx, ["status", "s1"]).stdout, "unknown", "status should report unknown");
});

test("status distinguishes a sampler that never ran from one with no reading", ({ ctx }) => {
  // The gate creates state without any sampler involvement, which is exactly
  // what a session started before the install looks like.
  gate(ctx, hookPayload(ctx));
  assertIncludes(
    cli(ctx, ["status", "s1"]).stdout,
    "has not run in this session",
    "it should name the real cause",
  );

  statusline(ctx, { session_id: "s1", cwd: ctx.project, model: { id: "claude-opus-5" } });
  const ran = cli(ctx, ["status", "s1"]).stdout;
  assertIncludes(ran, "no five-hour reading", "a sampler with no reading should say so instead");
  assertNotIncludes(ran, "has not run", "it must not still claim the sampler never ran");
});

test("a reading that appears after a gap does not reset an armed stage", ({ ctx }) => {
  sample(ctx, { percent: 93 });
  statusline(ctx, { session_id: "s1", cwd: ctx.project, model: { id: "claude-opus-5" } });
  expect(
    state(ctx).stage === "bailout-armed",
    "a missing reading must not disarm the bailout",
  ).toBeTruthy();
});

test("malformed input does not crash or corrupt state", ({ ctx }) => {
  sample(ctx, { percent: 88 });
  const before = readFileSync(path.join(ctx.home, "sessions", "s1.json"), "utf8");

  for (const bad of ["not json at all", '{"session_id":', "", "null", "[]"]) {
    const r = run(ctx, HOOKS, ["statusline"], bad);
    expect(
      r.status === 0,
      `statusline should exit 0 on malformed input, got ${r.status}`,
    ).toBeTruthy();
  }
  const g = run(ctx, HOOKS, ["gate"], "}{");
  expect(g.status === 0, "gate should exit 0 on malformed input").toBeTruthy();
  expect(g.stdout.trim() === "", "gate should print nothing on malformed input").toBeTruthy();

  expect(
    readFileSync(path.join(ctx.home, "sessions", "s1.json"), "utf8") === before,
    "state must be untouched",
  ).toBeTruthy();
});

test("a non-numeric percentage is ignored", ({ ctx }) => {
  statusline(ctx, {
    session_id: "s1",
    cwd: ctx.project,
    rate_limits: { five_hour: { used_percentage: "lots", resets_at: 2_000_000_000 } },
  });
  expect(state(ctx).stage === "idle", "a non-numeric reading must be ignored").toBeTruthy();
  expect(!state(ctx).usage, "a non-numeric reading must not be stored").toBeTruthy();
});

test("a stale sample reports as unknown rather than as a number", ({ ctx }) => {
  sample(ctx, { percent: 40 });
  const file = path.join(ctx.home, "sessions", "s1.json");
  const s = JSON.parse(readFileSync(file, "utf8"));
  s.usage.sampledAt -= 100_000;
  writeFileSync(file, JSON.stringify(s));
  const out = cli(ctx, ["status", "s1"]).stdout;
  assertIncludes(out, "stale", "status should flag the sample as stale");
  assertIncludes(out, "unknown", "a stale sample should read as unknown");
});

test("a corrupt state file is replaced rather than fatal", ({ ctx }) => {
  mkdirSync(path.join(ctx.home, "sessions"), { recursive: true });
  writeFileSync(path.join(ctx.home, "sessions", "s1.json"), "{{{ not json");
  const r = sample(ctx, { percent: 93 });
  expect(r.status === 0, "a corrupt state file should not be fatal").toBeTruthy();
  expect(
    state(ctx).stage === "bailout-armed",
    "state should be rebuilt and evaluated",
  ).toBeTruthy();
});

// ── quota window identity ──────────────────────────────────────────────────

test("a new quota window resets threshold state in explicitly armed sessions", ({ ctx }) => {
  sample(ctx, { percent: 96, resets: 2_000_000_000 });
  gate(ctx, hookPayload(ctx));
  writeFileSync(paths(ctx).handoff, "# handoff\n");
  stopHook(ctx, hookPayload(ctx));
  expect(state(ctx).stage === "bailout-done", "the handoff should settle the stage").toBeTruthy();

  // Same window, different session id: this is not evidence that usage reset.
  arm(ctx, "s2");
  sample(ctx, { session: "s2", percent: 96, resets: 2_000_000_000 });
  expect(
    state(ctx, "s2").stage === "bailout-armed",
    "a fresh session at 96% must still bail out",
  ).toBeTruthy();

  // Same session, later window with usage actually low again.
  sample(ctx, { percent: 12, resets: 2_000_018_000 });
  const after = state(ctx);
  expect(after.stage === "idle", `a new window should rearm, got ${after.stage}`).toBeTruthy();
  expect(after.window.resetsAt === 2_000_018_000, "the new window should be recorded").toBeTruthy();
  expect(
    Object.keys(after.handoffs).length === 0,
    "handoffs from the old window should not carry over",
  ).toBeTruthy();
});

test("a new window that is already high arms the bailout again", ({ ctx }) => {
  sample(ctx, { percent: 96, resets: 2_000_000_000 });
  gate(ctx, hookPayload(ctx));
  writeFileSync(paths(ctx).handoff, "# old handoff\n");
  stopHook(ctx, hookPayload(ctx));

  sample(ctx, { percent: 96, resets: 2_000_018_000 });
  expect(
    state(ctx).stage === "bailout-armed",
    "the new window should arm on its own reading",
  ).toBeTruthy();

  // The stale handoff.md from the previous window must not count as done.
  const old = paths(ctx).handoff;
  const past = new Date(Date.now() - 86_400_000);
  utimesSync(old, past, past);
  const result = stopHook(ctx, hookPayload(ctx));
  assertIncludes(
    result.stdout,
    '"block"',
    "a handoff from the previous window must not satisfy this one",
  );
});

// ── separation ─────────────────────────────────────────────────────────────

test("sessions and projects keep separate state and separate handoffs", ({ ctx }) => {
  const otherProject = path.join(ctx.root, "other-project");
  mkdirSync(otherProject, { recursive: true });
  arm(ctx, "a");
  arm(ctx, "b", otherProject);

  sample(ctx, { session: "a", percent: 93, cwd: ctx.project });
  sample(ctx, { session: "b", percent: 40, cwd: otherProject });

  expect(state(ctx, "a").stage === "bailout-armed", "session a should be armed").toBeTruthy();
  expect(state(ctx, "b").stage === "idle", "session b should be untouched").toBeTruthy();

  const pa = paths(ctx, "a");
  const pb = JSON.parse(cli(ctx, ["paths", "b", "--cwd", otherProject, "--json"]).stdout);
  expect(pa.handoff !== pb.handoff, "two sessions must not share a handoff path").toBeTruthy();
  assertIncludes(pa.handoff, "project", "the path should carry the project name");
  assertIncludes(pb.handoff, "other-project", "the path should carry the other project name");

  // Session b's gate must not pick up session a's instruction.
  expect(
    gate(ctx, hookPayload(ctx, "b")).stdout.trim() === "",
    "instructions must not leak between sessions",
  ).toBeTruthy();
});

// ── stop behaviour ─────────────────────────────────────────────────────────

test("stop blocks while the handoff is missing and steps aside once it exists", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  gate(ctx, hookPayload(ctx));

  const blocked = stopHook(ctx, hookPayload(ctx));
  const parsed = JSON.parse(blocked.stdout);
  expect(
    parsed.decision === "block",
    "stop should block while the handoff is missing",
  ).toBeTruthy();
  assertIncludes(parsed.reason, "does not exist yet", "the block reason should say why");
  expect(
    !("continue" in parsed),
    "stop must not halt Claude before it can write the handoff",
  ).toBeTruthy();

  writeFileSync(paths(ctx).handoff, "# handoff\n");
  const allowed = JSON.parse(stopHook(ctx, hookPayload(ctx)).stdout);
  // `continue: false` would stop the session short of the real usage limit, so
  // Claude Code would never pause it there and continue it after the reset.
  expect(
    !("continue" in allowed),
    "once the handoff exists stop must not end the session",
  ).toBeTruthy();
  expect(!("decision" in allowed), "stop must not block once the handoff exists").toBeTruthy();
  assertIncludes(allowed.systemMessage, paths(ctx).handoff, "the notice should name the handoff");
  expect(state(ctx).stage === "bailout-done", "the stage should settle").toBeTruthy();

  expect(
    stopHook(ctx, hookPayload(ctx)).stdout.trim() === "",
    "a later stop in the same window (a /goal loop) is left alone, with no repeat notice",
  ).toBeTruthy();
});

// Simulates the clock passing the window's reset time without a new reading.
function expireWindow(ctx, session = "s1") {
  const file = path.join(ctx.home, "sessions", `${session}.json`);
  const current = JSON.parse(readFileSync(file, "utf8"));
  current.window.resetsAt = Math.floor(Date.now() / 1000) - 60;
  writeFileSync(file, JSON.stringify(current));
}

test("a passed reset time ends the window before any new reading arrives", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  gate(ctx, hookPayload(ctx));
  expireWindow(ctx);

  // Claude Code continues the session after the reset; the old request must not
  // block that resumed turn or be delivered into it.
  expect(
    stopHook(ctx, hookPayload(ctx)).stdout.trim() === "",
    "a resumed turn must not be blocked for the previous window's handoff",
  ).toBeTruthy();
  expect(
    state(ctx).stage === "idle",
    "the passed reset should return the stage to idle",
  ).toBeTruthy();
  expect(
    gate(ctx, hookPayload(ctx)).stdout.trim() === "",
    "nothing from the previous window should be delivered",
  ).toBeTruthy();
});

test("an undelivered bailout does not survive a passed reset time", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  expireWindow(ctx);
  expect(
    gate(ctx, hookPayload(ctx)).stdout.trim() === "",
    "a request armed in the previous window must not reach the resumed session",
  ).toBeTruthy();
});

test("a reading from a window that has already reset is ignored", ({ ctx }) => {
  sample(ctx, { percent: 96, resets: Math.floor(Date.now() / 1000) - 60 });
  const after = state(ctx);
  expect(after.stage === "idle", "an outdated reading must not arm anything").toBeTruthy();
  expect(!after.usage, "an outdated reading must not be recorded as current").toBeTruthy();
});

test("stop blocks are bounded", ({ ctx }) => {
  writeConfig(ctx, { maxStopBlocks: 2 });
  sample(ctx, { percent: 96 });
  gate(ctx, hookPayload(ctx));

  for (let i = 1; i <= 2; i += 1) {
    const r = stopHook(ctx, hookPayload(ctx));
    assertIncludes(r.stdout, '"block"', `block ${i} should still fire`);
  }
  const third = JSON.parse(stopHook(ctx, hookPayload(ctx)).stdout);
  expect(
    !("continue" in third) && !("decision" in third),
    "the third stop must neither block nor end the session",
  ).toBeTruthy();
  assertIncludes(third.systemMessage, "not written", "the user should be told it was not written");
  expect(
    stopHook(ctx, hookPayload(ctx)).stdout.trim() === "",
    "the notice is not repeated on later stops",
  ).toBeTruthy();
});

test("a subagent does not receive the handoff instruction", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  const inSubagent = { ...hookPayload(ctx), agent_id: "sub-1", agent_type: "Explore" };
  expect(
    gate(ctx, inSubagent).stdout.trim() === "",
    "a subagent must not be asked to write the handoff",
  ).toBeTruthy();
  expect(state(ctx).pending, "the instruction must stay pending for the main session").toBeTruthy();
  assertIncludes(
    gate(ctx, hookPayload(ctx)).stdout,
    "handoff.md",
    "the main session still receives it",
  );
});

test("stop is silent for an ordinary end of turn", ({ ctx }) => {
  sample(ctx, { percent: 40 });
  const r = stopHook(ctx, hookPayload(ctx));
  expect(r.stdout.trim() === "", "stop must not summarise on an ordinary turn").toBeTruthy();
});

test("stop delivers the instruction when no tool call intervened", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  const r = stopHook(ctx, hookPayload(ctx));
  assertIncludes(r.stdout, "handoff.md", "stop should carry the instruction if the gate never ran");
});

// ── atomicity and recovery ─────────────────────────────────────────────────

test("a failed replacement write keeps the previous checkpoint", ({ ctx }) => {
  const p = paths(ctx);
  mkdirSync(p.directory, { recursive: true });
  writeFileSync(p.checkpoint, "# good checkpoint\nthe valuable one\n");

  chmodSync(p.directory, 0o500);
  let isThrew = false;
  try {
    writeFileSync(path.join(p.directory, "checkpoint.md.tmp.probe"), "x");
  } catch {
    isThrew = true;
  }
  const after = readFileSync(p.checkpoint, "utf8");
  chmodSync(p.directory, 0o700);

  expect(isThrew, "the fixture should make writes fail").toBeTruthy();
  assertIncludes(after, "the valuable one", "the previous checkpoint must survive a failed write");
  expect(
    readdirSync(p.directory).every((f) => !f.includes(".tmp.")),
    "no temp files should be left behind",
  ).toBeTruthy();
});

test("recovery preserves the checkpoint and is labelled as not written by Claude", ({ ctx }) => {
  sample(ctx, { percent: 93 });
  const p = paths(ctx);
  mkdirSync(p.directory, { recursive: true });
  writeFileSync(p.checkpoint, "# Checkpoint\n\nThe user wants the parser rewritten in Rust.\n");

  const out = cli(ctx, ["recover", "s1", "--cwd", ctx.project]).stdout.trim();
  expect(out === p.recovery, `recover should print the recovery path, got ${out}`).toBeTruthy();
  const body = readFileSync(p.recovery, "utf8");
  assertIncludes(body, "not written by Claude", "the snapshot must be labelled");
  assertIncludes(body, "rewritten in Rust", "the checkpoint content must be preserved");
  assertIncludes(body, "Continue the task described below", "the reader instruction must lead");
});

test("recovery works with no checkpoint and no git repository", ({ ctx }) => {
  const bare = path.join(ctx.root, "not-a-repo");
  mkdirSync(bare, { recursive: true });
  cli(ctx, ["recover", "solo", "--cwd", bare]);
  const body = readFileSync(
    path.join(ctx.home, "handoffs", "not-a-repo", "solo", "recovery.md"),
    "utf8",
  );
  assertIncludes(body, "No checkpoint was saved", "it should say the checkpoint is missing");
  assertIncludes(body, "Not a git repository", "it should handle a non-repository directory");
  assertNotIncludes(body, "undefined", "no undefined values should reach the file");
});

test("session end writes a recovery snapshot when the bailout was never answered", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  gate(ctx, hookPayload(ctx));
  sessionEnd(ctx, { session_id: "s1", cwd: ctx.project, hook_event_name: "SessionEnd" });

  expect(existsSync(paths(ctx).recovery), "a recovery snapshot should exist").toBeTruthy();
  expect(state(ctx).stage === "recovered", "the stage should record the recovery").toBeTruthy();
});

test("session end writes nothing when the handoff was already saved", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  gate(ctx, hookPayload(ctx));
  writeFileSync(paths(ctx).handoff, "# handoff\n");
  sessionEnd(ctx, { session_id: "s1", cwd: ctx.project, hook_event_name: "SessionEnd" });
  expect(
    !existsSync(paths(ctx).recovery),
    "no recovery snapshot is needed when the handoff exists",
  ).toBeTruthy();
});

// ── manual use ─────────────────────────────────────────────────────────────

test("manual paths work with no telemetry at all", ({ ctx }) => {
  const p = JSON.parse(cli(ctx, ["paths", "fresh", "--cwd", ctx.project, "--json"]).stdout);
  assertIncludes(p.handoff, "handoff.md", "paths should work without any sample");
  assertIncludes(p.checkpoint, "checkpoint.md", "paths should include the checkpoint file");
  expect(
    cli(ctx, ["status", "fresh"]).status === 0,
    "status should not fail without telemetry",
  ).toBeTruthy();
  assertIncludes(cli(ctx, ["status", "fresh"]).stdout, "unknown", "status should say unknown");
});

test("simulate drives the real sampler without touching the allowance", ({ ctx }) => {
  arm(ctx, "sim");
  const out = cli(ctx, [
    "simulate",
    "--session",
    "sim",
    "--percent",
    "96",
    "--cwd",
    ctx.project,
  ]).stdout;
  assertIncludes(out, "bailout-armed", "simulate should arm the bailout");
  expect(
    state(ctx, "sim").usage.usedPercentage === 96,
    "the simulated reading should be stored",
  ).toBeTruthy();
});

test("reset clears an armed stage", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  cli(ctx, ["reset", "s1", "--cwd", ctx.project]);
  expect(state(ctx).stage === "idle", "reset should return the session to idle").toBeTruthy();
  expect(
    gate(ctx, hookPayload(ctx)).stdout.trim() === "",
    "nothing should be pending after a reset",
  ).toBeTruthy();
});

// ── installation ───────────────────────────────────────────────────────────

const EXISTING_STATUSLINE = `#!/bin/bash
input=$(cat)
# pre-existing third-party bridge
printf '%s' "$input" > /dev/null
echo "MY CUSTOM STATUS LINE"
`;

function installFixture(ctx) {
  const settingsPath = path.join(ctx.root, "settings.json");
  const scriptPath = path.join(ctx.root, "existing-statusline.sh");
  writeFileSync(scriptPath, EXISTING_STATUSLINE);
  chmodSync(scriptPath, 0o755);
  writeFileSync(
    settingsPath,
    JSON.stringify(
      {
        model: "opus",
        statusLine: { type: "command", command: scriptPath },
        hooks: {
          PostToolUse: [
            { matcher: "*", hooks: [{ type: "command", command: "/existing/bridge" }] },
          ],
          Stop: [{ hooks: [{ type: "command", command: "/existing/bridge" }] }],
        },
        permissions: { allow: ["Bash(ls:*)"] },
      },
      null,
      2,
    ),
  );
  return { settingsPath, scriptPath };
}

function statuslineOutput(scriptPath) {
  const r = spawnSync("/bin/bash", [scriptPath], { input: "{}", encoding: "utf8" });
  return r.stdout.trim();
}

test("install merges into existing settings and preserves the status line", ({ ctx }) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  const before = statuslineOutput(scriptPath);

  const r = run(ctx, INSTALL, ["--settings", settingsPath]);
  expect(r.status === 0, `install should succeed: ${r.stderr}`).toBeTruthy();

  const settings = JSON.parse(readFileSync(settingsPath, "utf8"));
  expect(settings.model === "opus", "unrelated settings must survive").toBeTruthy();
  expect(settings.permissions.allow.length === 1, "permissions must be untouched").toBeTruthy();
  expect(
    settings.hooks.PostToolUse.some((g) => g.hooks.some((h) => h.command === "/existing/bridge")),
    "the pre-existing PostToolUse hook must survive",
  ).toBeTruthy();
  expect(
    settings.hooks.PostToolUse.some((g) => g.hooks.some((h) => h.command.includes("hooks.mjs"))),
    "the bailout gate should be registered",
  ).toBeTruthy();
  expect(settings.hooks.SessionEnd, "SessionEnd should be registered").toBeTruthy();
  expect(
    settings.statusLine.command === scriptPath,
    "the status line command must not change",
  ).toBeTruthy();

  assertIncludes(
    readFileSync(scriptPath, "utf8"),
    "bailout skill sampler",
    "the sampler block should be appended",
  );
  expect(
    statuslineOutput(scriptPath) === before,
    `status line output must be unchanged, got "${statuslineOutput(scriptPath)}"`,
  ).toBeTruthy();
});

test("install is repeatable without duplicating entries", ({ ctx }) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  run(ctx, INSTALL, ["--settings", settingsPath]);
  const afterFirst = readFileSync(scriptPath, "utf8");
  run(ctx, INSTALL, ["--settings", settingsPath]);
  run(ctx, INSTALL, ["--settings", settingsPath]);

  const settings = JSON.parse(readFileSync(settingsPath, "utf8"));
  const mine = settings.hooks.PostToolUse.filter((g) =>
    g.hooks.some((h) => h.command.includes("hooks.mjs")),
  );
  expect(
    mine.length === 1,
    `expected exactly one bailout gate group, got ${mine.length}`,
  ).toBeTruthy();
  expect(
    readFileSync(scriptPath, "utf8") === afterFirst,
    "the sampler block must not be appended twice",
  ).toBeTruthy();
});

test("uninstall removes only its own entries", ({ ctx }) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  const originalScript = readFileSync(scriptPath, "utf8");
  const before = statuslineOutput(scriptPath);

  run(ctx, INSTALL, ["--settings", settingsPath]);
  const r = run(ctx, INSTALL, ["--uninstall", "--settings", settingsPath]);
  expect(r.status === 0, `uninstall should succeed: ${r.stderr}`).toBeTruthy();

  const settings = JSON.parse(readFileSync(settingsPath, "utf8"));
  expect(settings.model === "opus", "unrelated settings must survive uninstall").toBeTruthy();
  expect(
    settings.statusLine.command === scriptPath,
    "the status line must stay configured",
  ).toBeTruthy();
  expect(
    settings.hooks.PostToolUse.every((g) => g.hooks.every((h) => !h.command.includes("hooks.mjs"))),
    "the bailout gate should be gone",
  ).toBeTruthy();
  expect(
    settings.hooks.PostToolUse.some((g) => g.hooks.some((h) => h.command === "/existing/bridge")),
    "the pre-existing hook must survive uninstall",
  ).toBeTruthy();
  expect(
    !settings.hooks.SessionEnd,
    "an event that only held a bailout hook should be removed",
  ).toBeTruthy();
  expect(
    readFileSync(scriptPath, "utf8") === originalScript,
    "the status line script should be restored byte for byte",
  ).toBeTruthy();
  expect(
    statuslineOutput(scriptPath) === before,
    "status line output must be restored",
  ).toBeTruthy();
});

test("uninstall restores a status line script that had no trailing newline", ({ ctx }) => {
  const settingsPath = path.join(ctx.root, "settings.json");
  const scriptPath = path.join(ctx.root, "no-newline-statusline.sh");
  const original = '#!/bin/bash\ninput=$(cat)\necho "STATUS"'; // deliberately no trailing newline
  writeFileSync(scriptPath, original);
  writeFileSync(
    settingsPath,
    JSON.stringify({ statusLine: { type: "command", command: scriptPath } }),
  );

  run(ctx, INSTALL, ["--settings", settingsPath]);
  assertIncludes(
    readFileSync(scriptPath, "utf8"),
    "bailout skill sampler",
    "the block should be appended",
  );

  run(ctx, INSTALL, ["--uninstall", "--settings", settingsPath]);
  expect(
    readFileSync(scriptPath, "utf8") === original,
    "the missing trailing newline must be restored too",
  ).toBeTruthy();
});

test("uninstall keeps a status line the user changed afterwards", ({ ctx }) => {
  const settingsPath = path.join(ctx.root, "settings.json");
  writeFileSync(settingsPath, JSON.stringify({}));
  run(ctx, INSTALL, ["--settings", settingsPath]);

  const created = JSON.parse(readFileSync(settingsPath, "utf8")).statusLine.command;
  assertIncludes(
    created,
    "statusline.sh",
    "a fallback status line should be created when none exists",
  );

  // The user then points the status line somewhere else.
  const settings = JSON.parse(readFileSync(settingsPath, "utf8"));
  settings.statusLine.command = "/my/own/statusline";
  writeFileSync(settingsPath, JSON.stringify(settings, null, 2));

  run(ctx, INSTALL, ["--uninstall", "--settings", settingsPath]);
  const after = JSON.parse(readFileSync(settingsPath, "utf8"));
  expect(
    after.statusLine.command === "/my/own/statusline",
    "a later user change must not be overwritten",
  ).toBeTruthy();
});

test("install backs up what it changes and dry run writes nothing", ({ ctx }) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  const before = readFileSync(settingsPath, "utf8");

  const dry = run(ctx, INSTALL, ["--settings", settingsPath, "--dry-run"]);
  assertIncludes(dry.stdout, "dry run", "a dry run should say so");
  expect(
    readFileSync(settingsPath, "utf8") === before,
    "a dry run must not change settings",
  ).toBeTruthy();
  expect(
    !readFileSync(scriptPath, "utf8").includes("bailout skill sampler"),
    "a dry run must not change the script",
  ).toBeTruthy();
  expect(
    readdirSync(ctx.root).every((f) => !f.includes(".bailout-bak.")),
    "a dry run must not leave backup files either",
  ).toBeTruthy();

  run(ctx, INSTALL, ["--settings", settingsPath]);
  const backups = readdirSync(ctx.root).filter((f) => f.includes(".bailout-bak."));
  expect(
    backups.length >= 2,
    `expected backups of both files, got ${backups.join(", ")}`,
  ).toBeTruthy();
});

test("reinstall repairs a sampler block whose interpreter path has gone stale", ({ ctx }) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  run(ctx, INSTALL, ["--settings", settingsPath]);

  // Simulate a node upgrade removing the pinned interpreter the block was
  // installed with. The sampler discards stderr, so this would fail silently.
  const stale = readFileSync(scriptPath, "utf8").replace(
    /"[^"]*node"/,
    '"/opt/homebrew/Cellar/node/0.0.0/bin/node"',
  );
  writeFileSync(scriptPath, stale);
  assertIncludes(readFileSync(scriptPath, "utf8"), "0.0.0", "the fixture should be stale");

  const r = run(ctx, INSTALL, ["--settings", settingsPath]);
  assertIncludes(r.stdout, "refreshed", "a reinstall should report that it repaired the block");
  assertNotIncludes(
    readFileSync(scriptPath, "utf8"),
    "0.0.0",
    "the stale interpreter should be gone",
  );
  assertIncludes(
    readFileSync(scriptPath, "utf8"),
    "MY CUSTOM STATUS LINE",
    "the rest of the script must survive",
  );
  expect(
    (readFileSync(scriptPath, "utf8").match(/bailout skill sampler \(managed/g) || []).length === 1,
    "repairing must not leave two blocks behind",
  ).toBeTruthy();
});

test("status reports a broken install instead of failing silently", ({ ctx }) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  assertIncludes(
    cli(ctx, ["status", "s1"]).stdout,
    "not installed",
    "an uninstalled state should say so",
  );

  run(ctx, INSTALL, ["--settings", settingsPath]);
  assertIncludes(
    cli(ctx, ["status", "s1"]).stdout,
    "install:    ok",
    "a healthy install should report ok",
  );

  writeFileSync(
    scriptPath,
    readFileSync(scriptPath, "utf8").replace(/"[^"]*node"/, '"/nonexistent/node"'),
  );
  const broken = cli(ctx, ["status", "s1"]).stdout;
  assertIncludes(broken, "PROBLEM", "a missing interpreter should be reported");
  assertIncludes(broken, "/nonexistent/node", "the report should name the missing path");
});

test("install reports rather than guesses when the status line cannot take the block", ({
  ctx,
}) => {
  const settingsPath = path.join(ctx.root, "settings.json");
  const scriptPath = path.join(ctx.root, "opaque-statusline");
  writeFileSync(scriptPath, "#!/bin/bash\necho hi\n");
  writeFileSync(
    settingsPath,
    JSON.stringify({ statusLine: { type: "command", command: scriptPath } }),
  );

  const r = run(ctx, INSTALL, ["--settings", settingsPath]);
  assertIncludes(r.stdout, "manual-required", "it should report that manual wiring is needed");
  assertIncludes(r.stdout, "statusline", "it should print the line to add");
  expect(
    readFileSync(scriptPath, "utf8") === "#!/bin/bash\necho hi\n",
    "an unrecognised script must not be edited",
  ).toBeTruthy();
});

// ── the central sequence ───────────────────────────────────────────────────

test("INTEGRATION: a continuing task crosses the threshold, hands off, and carries on", ({
  ctx,
}) => {
  // A task is under way. Several turns pass below the thresholds; the user sends
  // nothing, and nothing is asked of Claude.
  for (const percent of [55, 70, 84]) {
    sample(ctx, { percent });
    expect(
      gate(ctx, hookPayload(ctx)).stdout.trim() === "",
      `no request expected at ${percent}%`,
    ).toBeTruthy();
    expect(
      stopHook(ctx, hookPayload(ctx)).stdout.trim() === "",
      `no stop block expected at ${percent}%`,
    ).toBeTruthy();
  }

  // The five-hour allowance crosses the checkpoint threshold mid-task.
  sample(ctx, { percent: 88 });
  const checkpointAsk = additionalContext(gate(ctx, hookPayload(ctx)));
  assertIncludes(checkpointAsk, "checkpoint.md", "a checkpoint should be requested");
  writeFileSync(paths(ctx).checkpoint, "# Checkpoint\n\nObjective: ship the importer.\n");

  // Ordinary work continues after a checkpoint.
  expect(
    stopHook(ctx, hookPayload(ctx)).stdout.trim() === "",
    "a checkpoint must not stop the turn",
  ).toBeTruthy();
  sample(ctx, { percent: 90 });
  expect(
    gate(ctx, hookPayload(ctx)).stdout.trim() === "",
    "the checkpoint must not be requested again",
  ).toBeTruthy();

  // The bailout threshold is crossed, still with no new user prompt.
  sample(ctx, { percent: 93 });
  const bailoutAsk = additionalContext(gate(ctx, hookPayload(ctx)));
  assertIncludes(bailoutAsk, "handoff.md", "the handoff should be requested");
  assertIncludes(
    bailoutAsk,
    "checkpoint.md",
    "the existing checkpoint should be offered to build on",
  );
  assertIncludes(bailoutAsk, "carries on", "the work should carry on after the handoff");

  // Claude writes the handoff first, then carries on with the task.
  writeFileSync(
    paths(ctx).handoff,
    "# Handoff\n\nObjective: ship the importer.\nNext: run the tests.\n",
  );

  // The turn is neither blocked nor ended by the hook; the user is told once.
  const ended = JSON.parse(stopHook(ctx, hookPayload(ctx)).stdout);
  expect(
    !("continue" in ended) && !("decision" in ended),
    "the hook must neither block the turn nor end the session",
  ).toBeTruthy();
  assertIncludes(ended.systemMessage, "handoff saved", "the user should see where it went");
  expect(state(ctx).stage === "bailout-done", "the stage should be settled").toBeTruthy();

  // Work runs on towards the real limit. Nothing further is asked, and nothing
  // ends the session before Claude Code's own usage-limit pause can.
  sample(ctx, { percent: 97 });
  expect(
    gate(ctx, hookPayload(ctx)).stdout.trim() === "",
    "no further request after the handoff",
  ).toBeTruthy();
  expect(
    stopHook(ctx, hookPayload(ctx)).stdout.trim() === "",
    "later stops are left to Claude Code and any /goal",
  ).toBeTruthy();

  // Claude Code continues the session once the window resets. The finished
  // bailout belongs to the old window and does not touch the resumed turn.
  expireWindow(ctx);
  expect(
    stopHook(ctx, hookPayload(ctx)).stdout.trim() === "",
    "the resumed turn should see nothing from the bailout",
  ).toBeTruthy();
  sample(ctx, { percent: 3, resets: 2_000_018_000 });
  expect(state(ctx).stage === "idle", "the new window should start idle").toBeTruthy();

  // Session end needs no recovery snapshot, because a real handoff exists.
  sessionEnd(ctx, { session_id: "s1", cwd: ctx.project });
  expect(
    !existsSync(paths(ctx).recovery),
    "no recovery snapshot when a real handoff was written",
  ).toBeTruthy();
});

// Explicit opt-in: inactive fixtures never call the arm command implicitly.
inactiveTest("all hooks stay silent and create no state before explicit arming", ({ ctx }) => {
  writeConfig(ctx, { enabled: true });
  expect(sample(ctx, { percent: 99 }).stdout).toBe("");
  for (const hook of [gate, stopHook, sessionEnd]) {
    expect(hook(ctx, hookPayload(ctx)).stdout).toBe("");
  }
  expect(existsSync(path.join(ctx.home, "sessions"))).toBe(false);
  expect(existsSync(paths(ctx).directory)).toBe(false);
  expect(cli(ctx, ["status", "s1", "--cwd", ctx.project]).stdout).toContain("arming:     disarmed");
});

inactiveTest("legacy threshold state and enabled config never opt a session in", ({ ctx }) => {
  writeConfig(ctx, { enabled: true });
  mkdirSync(path.join(ctx.home, "sessions"));
  const file = path.join(ctx.home, "sessions", "s1.json");
  const legacy = JSON.stringify({
    schema: 1,
    sessionId: "s1",
    cwd: ctx.project,
    stage: "bailout-armed",
    pending: { kind: "bailout" },
    armed: { bailout: 1 },
    handoffs: {},
    stopBlocks: 0,
    usage: { usedPercentage: 99, sampledAt: Math.floor(Date.now() / 1000) },
  });
  writeFileSync(file, legacy);
  sample(ctx, { percent: 99 });
  for (const hook of [gate, stopHook, sessionEnd])
    expect(hook(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(readFileSync(file, "utf8")).toBe(legacy);
  expect(existsSync(paths(ctx).recovery)).toBe(false);
});

inactiveTest("arming requires an actual session and a valid directory", ({ ctx }) => {
  for (const command of ["arm", "disarm"]) {
    const result = cli(ctx, [command]);
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("needs a session ID");
  }
  expect(cli(ctx, ["arm", "s1", "--cwd", path.join(ctx.root, "missing")]).status).toBe(1);
  expect(existsSync(path.join(ctx.home, "arming"))).toBe(false);
  expect(existsSync(path.join(ctx.home, "sessions"))).toBe(false);
});

inactiveTest("the explicit arm command enables only that session and directory", ({ ctx }) => {
  arm(ctx);
  expect(cli(ctx, ["status", "s1", "--cwd", ctx.project]).stdout).toContain("arming:     armed");
  sample(ctx, { percent: 96, session: "another-session" });
  expect(gate(ctx, hookPayload(ctx, "another-session")).stdout).toBe("");
  expect(existsSync(path.join(ctx.home, "sessions", "another-session.json"))).toBe(false);

  const otherProject = path.join(ctx.root, "other-project");
  mkdirSync(otherProject);
  sample(ctx, { percent: 96, cwd: otherProject });
  for (const hook of [gate, stopHook]) {
    expect(hook(ctx, { ...hookPayload(ctx), cwd: otherProject }).stdout).toBe("");
  }
  expect(existsSync(path.join(ctx.home, "handoffs"))).toBe(false);
  sample(ctx, { percent: 96 });
  const instruction = additionalContext(gate(ctx, hookPayload(ctx)));
  expect(instruction).toContain("handoff.md");
});

inactiveTest("arming discards old pending requests and waits for a new sample", ({ ctx }) => {
  mkdirSync(path.join(ctx.home, "sessions"));
  writeFileSync(
    path.join(ctx.home, "sessions", "s1.json"),
    JSON.stringify({
      sessionId: "s1",
      cwd: ctx.project,
      stage: "bailout-armed",
      pending: { kind: "bailout" },
    }),
  );
  arm(ctx);
  expect(gate(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(stopHook(ctx, hookPayload(ctx)).stdout).toBe("");
  sample(ctx, { percent: 96 });
  const instruction = additionalContext(gate(ctx, hookPayload(ctx)));
  expect(instruction).toContain("handoff.md");
});

test("repeating arm leaves an already delivered request settled", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  gate(ctx, hookPayload(ctx));
  writeFileSync(paths(ctx).handoff, "# saved handoff\n");
  stopHook(ctx, hookPayload(ctx));
  arm(ctx);
  sample(ctx, { percent: 96 });
  expect(gate(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(stopHook(ctx, hookPayload(ctx)).stdout).toBe("");
});

test("disarm cancels pending delivery, stop blocks and automatic recovery", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  expect(cli(ctx, ["disarm", "s1", "--cwd", ctx.project]).status).toBe(0);
  sample(ctx, { percent: 99 });
  for (const hook of [gate, stopHook, sessionEnd])
    expect(hook(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(existsSync(paths(ctx).recovery)).toBe(false);
  expect(cli(ctx, ["status", "s1", "--cwd", ctx.project]).stdout).toContain("arming:     disarmed");
  arm(ctx);
  sample(ctx, { percent: 96 });
  const instruction = additionalContext(gate(ctx, hookPayload(ctx)));
  expect(instruction).toContain("handoff.md");
});

test("reset also disarms rather than silently restarting monitoring", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  cli(ctx, ["reset", "s1", "--cwd", ctx.project]);
  sample(ctx, { percent: 99 });
  expect(gate(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(stopHook(ctx, hookPayload(ctx)).stdout).toBe("");
});

test("session end saves outstanding recovery then clears the opt-in", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  sessionEnd(ctx, hookPayload(ctx));
  expect(existsSync(paths(ctx).recovery)).toBe(true);
  const before = readFileSync(paths(ctx).recovery, "utf8");
  sample(ctx, { percent: 99 });
  expect(gate(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(stopHook(ctx, hookPayload(ctx)).stdout).toBe("");
  sessionEnd(ctx, hookPayload(ctx));
  expect(readFileSync(paths(ctx).recovery, "utf8")).toBe(before);
  expect(cli(ctx, ["status", "s1", "--cwd", ctx.project]).stdout).toContain("arming:     disarmed");
});

test("ordinary session end clears arming without creating recovery", ({ ctx }) => {
  sample(ctx, { percent: 40 });
  sessionEnd(ctx, hookPayload(ctx));
  sample(ctx, { percent: 99 });
  expect(gate(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(existsSync(paths(ctx).recovery)).toBe(false);
});

test("session end clears opt-in even when config disables hooks", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  writeConfig(ctx, { enabled: false });
  sessionEnd(ctx, hookPayload(ctx));
  writeConfig(ctx, { enabled: true });
  sample(ctx, { percent: 99 });
  expect(gate(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(stopHook(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(existsSync(paths(ctx).recovery)).toBe(false);
});

inactiveTest(
  "disabled config refuses arming and enabling config still requires opt-in",
  ({ ctx }) => {
    writeConfig(ctx, { enabled: false });
    expect(cli(ctx, ["arm", "s1", "--cwd", ctx.project]).status).toBe(1);
    writeConfig(ctx, { enabled: true });
    sample(ctx, { percent: 99 });
    expect(gate(ctx, hookPayload(ctx)).stdout).toBe("");
  },
);

test("subagent stop and end cannot act on or disarm the parent session", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  const payload = { ...hookPayload(ctx), agent_id: "child" };
  expect(stopHook(ctx, payload).stdout).toBe("");
  expect(sessionEnd(ctx, payload).stdout).toBe("");
  const instruction = additionalContext(gate(ctx, hookPayload(ctx)));
  expect(instruction).toContain("handoff.md");
});

inactiveTest("installation leaves new and legacy sessions unarmed", ({ ctx }) => {
  const { settingsPath } = installFixture(ctx);
  const installed = run(ctx, INSTALL, ["--settings", settingsPath]);
  expect(installed.status).toBe(0);
  expect(installed.stdout).toContain("stay off until you explicitly arm");
  sample(ctx, { percent: 99 });
  expect(gate(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(stopHook(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(existsSync(path.join(ctx.home, "arming"))).toBe(false);
});

inactiveTest("manual recovery and paths never arm monitoring", ({ ctx }) => {
  expect(cli(ctx, ["recover", "s1", "--cwd", ctx.project]).status).toBe(0);
  expect(existsSync(paths(ctx).recovery)).toBe(true);
  sample(ctx, { percent: 99 });
  expect(gate(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(stopHook(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(cli(ctx, ["status", "s1", "--cwd", ctx.project]).stdout).toContain("arming:     disarmed");
});

test("hooks with an unknown directory cannot act on saved session context", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  const unknown = { session_id: "s1" };
  expect(gate(ctx, unknown).stdout).toBe("");
  expect(stopHook(ctx, unknown).stdout).toBe("");
  const instruction = additionalContext(gate(ctx, hookPayload(ctx)));
  expect(instruction).toContain("handoff.md");
  sessionEnd(ctx, unknown);
  expect(existsSync(paths(ctx).recovery)).toBe(false);
  sample(ctx, { percent: 99 });
  expect(stopHook(ctx, hookPayload(ctx)).stdout).toBe("");
});

test("a corrupt opt-in file cannot leave pending requests active", ({ ctx }) => {
  sample(ctx, { percent: 96 });
  const directory = path.join(ctx.home, "arming");
  for (const file of readdirSync(directory)) writeFileSync(path.join(directory, file), "{bad json");
  expect(gate(ctx, hookPayload(ctx)).stdout).toBe("");
  expect(stopHook(ctx, hookPayload(ctx)).stdout).toBe("");
  sessionEnd(ctx, hookPayload(ctx));
  expect(existsSync(paths(ctx).recovery)).toBe(false);
});
