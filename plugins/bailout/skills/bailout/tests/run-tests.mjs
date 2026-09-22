#!/usr/bin/env node
// Fixture-driven tests for the bailout skill. Every test runs against a fresh
// temporary BAILOUT_HOME and fake settings files, so none of this touches the
// real configuration and none of it spends any actual allowance.
//
//   node tests/run-tests.mjs [name-filter]

import { spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const TESTS_DIR = dirname(fileURLToPath(import.meta.url));
const SKILL_DIR = dirname(TESTS_DIR);
const HOOKS = join(SKILL_DIR, 'scripts', 'hooks.mjs');
const CLI = join(SKILL_DIR, 'scripts', 'bailout.mjs');
const INSTALL = join(SKILL_DIR, 'scripts', 'install.mjs');

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function assertIncludes(haystack, needle, message) {
  assert(String(haystack).includes(needle), `${message}\n  expected to contain: ${needle}\n  got: ${String(haystack).slice(0, 600)}`);
}

function assertNotIncludes(haystack, needle, message) {
  assert(!String(haystack).includes(needle), `${message}\n  expected NOT to contain: ${needle}`);
}

// ── harness ────────────────────────────────────────────────────────────────

function makeContext() {
  const root = mkdtempSync(join(tmpdir(), 'bailout-test-'));
  const home = join(root, 'home');
  const project = join(root, 'project');
  mkdirSync(home, { recursive: true });
  mkdirSync(project, { recursive: true });
  return { root, home, project, env: { ...process.env, BAILOUT_HOME: home } };
}

function run(ctx, script, args, input) {
  const result = spawnSync(process.execPath, [script, ...args], {
    input: input === undefined ? '' : input,
    encoding: 'utf8',
    env: ctx.env,
  });
  return { stdout: result.stdout || '', stderr: result.stderr || '', status: result.status };
}

function statusline(ctx, payload) {
  return run(ctx, HOOKS, ['statusline'], JSON.stringify(payload));
}

function gate(ctx, payload) {
  return run(ctx, HOOKS, ['gate'], JSON.stringify(payload));
}

function stopHook(ctx, payload) {
  return run(ctx, HOOKS, ['stop'], JSON.stringify(payload));
}

function sessionEnd(ctx, payload) {
  return run(ctx, HOOKS, ['session-end'], JSON.stringify(payload));
}

function cli(ctx, args) {
  return run(ctx, CLI, args);
}

function sample(ctx, { session = 's1', percent, model = 'claude-opus-5', resets = 2000000000, cwd }) {
  return statusline(ctx, {
    session_id: session,
    cwd: cwd || ctx.project,
    transcript_path: join(ctx.root, 'transcript.jsonl'),
    version: '2.1.276',
    model: { id: model, display_name: model.includes('opus') ? 'Opus' : 'Model' },
    rate_limits: { five_hour: { used_percentage: percent, resets_at: resets } },
  });
}

function hookPayload(ctx, session = 's1') {
  return { session_id: session, cwd: ctx.project, hook_event_name: 'PostToolUse' };
}

function state(ctx, session = 's1') {
  return JSON.parse(readFileSync(join(ctx.home, 'sessions', `${session}.json`), 'utf8'));
}

function paths(ctx, session = 's1') {
  const out = cli(ctx, ['paths', session, '--cwd', ctx.project, '--json']).stdout;
  return JSON.parse(out);
}

function writeConfig(ctx, config) {
  mkdirSync(ctx.home, { recursive: true });
  writeFileSync(join(ctx.home, 'config.json'), JSON.stringify(config, null, 2));
}

function additionalContext(result) {
  if (!result.stdout.trim()) return null;
  return JSON.parse(result.stdout).hookSpecificOutput?.additionalContext ?? null;
}

// ── threshold behaviour ────────────────────────────────────────────────────

test('below every threshold nothing arms and the gate stays silent', (ctx) => {
  sample(ctx, { percent: 40 });
  assert(state(ctx).stage === 'idle', 'stage should stay idle');
  const result = gate(ctx, hookPayload(ctx));
  assert(result.stdout.trim() === '', 'gate should print nothing below thresholds');
  assert(result.status === 0, 'gate should exit 0');
});

test('checkpoint threshold arms once and the gate delivers it once', (ctx) => {
  // Opus margin is 3, so the checkpoint threshold is 87.
  sample(ctx, { percent: 88 });
  assert(state(ctx).stage === 'checkpoint-armed', `expected checkpoint-armed, got ${state(ctx).stage}`);

  const first = additionalContext(gate(ctx, hookPayload(ctx)));
  assertIncludes(first, 'checkpoint.md', 'checkpoint instruction should name the checkpoint file');
  assertIncludes(first, '88%', 'instruction should state the observed percentage');
  assert(state(ctx).stage === 'checkpoint-delivered', 'stage should advance after delivery');

  const second = gate(ctx, hookPayload(ctx));
  assert(second.stdout.trim() === '', 'the checkpoint must not be requested twice');
});

test('repeated samples at the same level do not re-arm', (ctx) => {
  sample(ctx, { percent: 88 });
  gate(ctx, hookPayload(ctx));
  for (let i = 0; i < 5; i += 1) sample(ctx, { percent: 88 + i * 0.1 });
  const result = gate(ctx, hookPayload(ctx));
  assert(result.stdout.trim() === '', 'repeated samples must not produce repeated requests');
});

test('bailout threshold arms and overrides an undelivered checkpoint', (ctx) => {
  sample(ctx, { percent: 88 });
  sample(ctx, { percent: 93 });
  assert(state(ctx).stage === 'bailout-armed', `expected bailout-armed, got ${state(ctx).stage}`);
  const text = additionalContext(gate(ctx, hookPayload(ctx)));
  assertIncludes(text, 'handoff.md', 'bailout instruction should name the handoff file');
  assertIncludes(text, 'no new ordinary work is started', 'bailout instruction should say to stop ordinary work');
});

test('a session that starts already above the bailout threshold arms immediately', (ctx) => {
  sample(ctx, { percent: 97 });
  assert(state(ctx).stage === 'bailout-armed', `expected bailout-armed, got ${state(ctx).stage}`);
  const text = additionalContext(gate(ctx, hookPayload(ctx)));
  assertIncludes(text, 'handoff.md', 'should go straight to the final handoff');
  assertNotIncludes(text, 'checkpoint.md', 'should not ask for a checkpoint first');
});

test('the model margin moves the threshold and heavier models bail out earlier', (ctx) => {
  sample(ctx, { session: 'opus', percent: 92.5, model: 'claude-opus-5' });
  assert(state(ctx, 'opus').stage === 'bailout-armed', 'opus should bail out at 92.5 (threshold 92)');

  sample(ctx, { session: 'sonnet', percent: 92.5, model: 'claude-sonnet-5' });
  assert(state(ctx, 'sonnet').stage === 'checkpoint-armed', 'sonnet threshold is 94, so 92.5 is only a checkpoint');

  sample(ctx, { session: 'haiku', percent: 94.5, model: 'claude-haiku-4-5' });
  assert(state(ctx, 'haiku').stage === 'checkpoint-armed', 'haiku keeps the unadjusted 95 threshold');

  sample(ctx, { session: 'haiku', percent: 95.5, model: 'claude-haiku-4-5' });
  assert(state(ctx, 'haiku').stage === 'bailout-armed', 'haiku bails out at 95');
});

test('the early checkpoint can be disabled without disabling the bailout', (ctx) => {
  writeConfig(ctx, { checkpointEnabled: false });
  sample(ctx, { percent: 88 });
  assert(state(ctx).stage === 'idle', 'no checkpoint should arm when it is disabled');
  sample(ctx, { percent: 93 });
  assert(state(ctx).stage === 'bailout-armed', 'the bailout should still arm');
});

test('thresholds are configurable', (ctx) => {
  writeConfig(ctx, { bailoutPercent: 60, checkpointPercent: 50, modelMarginPercent: { default: 0, opus: 0 } });
  sample(ctx, { percent: 55 });
  assert(state(ctx).stage === 'checkpoint-armed', 'custom checkpoint threshold should apply');
  sample(ctx, { percent: 61 });
  assert(state(ctx).stage === 'bailout-armed', 'custom bailout threshold should apply');
});

// ── bad, missing and stale data ────────────────────────────────────────────

test('missing rate_limits is unknown, not zero', (ctx) => {
  statusline(ctx, { session_id: 's1', cwd: ctx.project, model: { id: 'claude-opus-5' } });
  const s = state(ctx);
  assert(s.stage === 'idle', 'stage should stay idle without a reading');
  assert(!s.usage, 'no usage should be recorded');
  assertIncludes(cli(ctx, ['status', 's1']).stdout, 'unknown', 'status should report unknown');
});

test('status distinguishes a sampler that never ran from one with no reading', (ctx) => {
  // The gate creates state without any sampler involvement, which is exactly
  // what a session started before the install looks like.
  gate(ctx, hookPayload(ctx));
  assertIncludes(cli(ctx, ['status', 's1']).stdout, 'has not run in this session', 'it should name the real cause');

  statusline(ctx, { session_id: 's1', cwd: ctx.project, model: { id: 'claude-opus-5' } });
  const ran = cli(ctx, ['status', 's1']).stdout;
  assertIncludes(ran, 'no five-hour reading', 'a sampler with no reading should say so instead');
  assertNotIncludes(ran, 'has not run', 'it must not still claim the sampler never ran');
});

test('a reading that appears after a gap does not reset an armed stage', (ctx) => {
  sample(ctx, { percent: 93 });
  statusline(ctx, { session_id: 's1', cwd: ctx.project, model: { id: 'claude-opus-5' } });
  assert(state(ctx).stage === 'bailout-armed', 'a missing reading must not disarm the bailout');
});

test('malformed input does not crash or corrupt state', (ctx) => {
  sample(ctx, { percent: 88 });
  const before = readFileSync(join(ctx.home, 'sessions', 's1.json'), 'utf8');

  for (const bad of ['not json at all', '{"session_id":', '', 'null', '[]']) {
    const r = run(ctx, HOOKS, ['statusline'], bad);
    assert(r.status === 0, `statusline should exit 0 on malformed input, got ${r.status}`);
  }
  const g = run(ctx, HOOKS, ['gate'], '}{');
  assert(g.status === 0, 'gate should exit 0 on malformed input');
  assert(g.stdout.trim() === '', 'gate should print nothing on malformed input');

  assert(readFileSync(join(ctx.home, 'sessions', 's1.json'), 'utf8') === before, 'state must be untouched');
});

test('a non-numeric percentage is ignored', (ctx) => {
  statusline(ctx, {
    session_id: 's1',
    cwd: ctx.project,
    rate_limits: { five_hour: { used_percentage: 'lots', resets_at: 2000000000 } },
  });
  assert(state(ctx).stage === 'idle', 'a non-numeric reading must be ignored');
  assert(!state(ctx).usage, 'a non-numeric reading must not be stored');
});

test('a stale sample reports as unknown rather than as a number', (ctx) => {
  sample(ctx, { percent: 40 });
  const file = join(ctx.home, 'sessions', 's1.json');
  const s = JSON.parse(readFileSync(file, 'utf8'));
  s.usage.sampledAt -= 100000;
  writeFileSync(file, JSON.stringify(s));
  const out = cli(ctx, ['status', 's1']).stdout;
  assertIncludes(out, 'stale', 'status should flag the sample as stale');
  assertIncludes(out, 'unknown', 'a stale sample should read as unknown');
});

test('a corrupt state file is replaced rather than fatal', (ctx) => {
  mkdirSync(join(ctx.home, 'sessions'), { recursive: true });
  writeFileSync(join(ctx.home, 'sessions', 's1.json'), '{{{ not json');
  const r = sample(ctx, { percent: 93 });
  assert(r.status === 0, 'a corrupt state file should not be fatal');
  assert(state(ctx).stage === 'bailout-armed', 'state should be rebuilt and evaluated');
});

// ── quota window identity ──────────────────────────────────────────────────

test('a new quota window rearms, a new session does not', (ctx) => {
  sample(ctx, { percent: 96, resets: 1000 });
  gate(ctx, hookPayload(ctx));
  writeFileSync(paths(ctx).handoff, '# handoff\n');
  stopHook(ctx, hookPayload(ctx));
  assert(state(ctx).stage === 'bailout-done', 'the handoff should settle the stage');

  // Same window, different session id: this is not evidence that usage reset.
  sample(ctx, { session: 's2', percent: 96, resets: 1000 });
  assert(state(ctx, 's2').stage === 'bailout-armed', 'a fresh session at 96% must still bail out');

  // Same session, later window with usage actually low again.
  sample(ctx, { percent: 12, resets: 2000 });
  const after = state(ctx);
  assert(after.stage === 'idle', `a new window should rearm, got ${after.stage}`);
  assert(after.window.resetsAt === 2000, 'the new window should be recorded');
  assert(Object.keys(after.handoffs).length === 0, 'handoffs from the old window should not carry over');
});

test('a new window that is already high arms the bailout again', (ctx) => {
  sample(ctx, { percent: 96, resets: 1000 });
  gate(ctx, hookPayload(ctx));
  writeFileSync(paths(ctx).handoff, '# old handoff\n');
  stopHook(ctx, hookPayload(ctx));

  sample(ctx, { percent: 96, resets: 5000 });
  assert(state(ctx).stage === 'bailout-armed', 'the new window should arm on its own reading');

  // The stale handoff.md from the previous window must not count as done.
  const old = paths(ctx).handoff;
  const past = new Date(Date.now() - 86400000);
  utimesSync(old, past, past);
  const result = stopHook(ctx, hookPayload(ctx));
  assertIncludes(result.stdout, '"block"', 'a handoff from the previous window must not satisfy this one');
});

// ── separation ─────────────────────────────────────────────────────────────

test('sessions and projects keep separate state and separate handoffs', (ctx) => {
  const otherProject = join(ctx.root, 'other-project');
  mkdirSync(otherProject, { recursive: true });

  sample(ctx, { session: 'a', percent: 93, cwd: ctx.project });
  sample(ctx, { session: 'b', percent: 40, cwd: otherProject });

  assert(state(ctx, 'a').stage === 'bailout-armed', 'session a should be armed');
  assert(state(ctx, 'b').stage === 'idle', 'session b should be untouched');

  const pa = paths(ctx, 'a');
  const pb = JSON.parse(cli(ctx, ['paths', 'b', '--cwd', otherProject, '--json']).stdout);
  assert(pa.handoff !== pb.handoff, 'two sessions must not share a handoff path');
  assertIncludes(pa.handoff, 'project', 'the path should carry the project name');
  assertIncludes(pb.handoff, 'other-project', 'the path should carry the other project name');

  // Session b's gate must not pick up session a's instruction.
  assert(gate(ctx, hookPayload(ctx, 'b')).stdout.trim() === '', 'instructions must not leak between sessions');
});

// ── stop behaviour ─────────────────────────────────────────────────────────

test('stop blocks while the handoff is missing and yields once it exists', (ctx) => {
  sample(ctx, { percent: 96 });
  gate(ctx, hookPayload(ctx));

  const blocked = stopHook(ctx, hookPayload(ctx));
  const parsed = JSON.parse(blocked.stdout);
  assert(parsed.decision === 'block', 'stop should block while the handoff is missing');
  assertIncludes(parsed.reason, 'does not exist yet', 'the block reason should say why');
  assert(!('continue' in parsed), 'stop must not halt Claude before it can write the handoff');

  writeFileSync(paths(ctx).handoff, '# handoff\n');
  const allowed = stopHook(ctx, hookPayload(ctx));
  assert(allowed.stdout.trim() === '', 'stop should yield once the handoff exists');
  assert(state(ctx).stage === 'bailout-done', 'the stage should settle');
});

test('stop blocks are bounded', (ctx) => {
  writeConfig(ctx, { maxStopBlocks: 2 });
  sample(ctx, { percent: 96 });
  gate(ctx, hookPayload(ctx));

  for (let i = 1; i <= 2; i += 1) {
    const r = stopHook(ctx, hookPayload(ctx));
    assertIncludes(r.stdout, '"block"', `block ${i} should still fire`);
  }
  const third = stopHook(ctx, hookPayload(ctx));
  assert(third.stdout.trim() === '', 'the third stop must be allowed through');
});

test('a subagent does not receive the handoff instruction', (ctx) => {
  sample(ctx, { percent: 96 });
  const inSubagent = { ...hookPayload(ctx), agent_id: 'sub-1', agent_type: 'Explore' };
  assert(gate(ctx, inSubagent).stdout.trim() === '', 'a subagent must not be asked to write the handoff');
  assert(state(ctx).pending, 'the instruction must stay pending for the main session');
  assertIncludes(gate(ctx, hookPayload(ctx)).stdout, 'handoff.md', 'the main session still receives it');
});

test('stop is silent for an ordinary end of turn', (ctx) => {
  sample(ctx, { percent: 40 });
  const r = stopHook(ctx, hookPayload(ctx));
  assert(r.stdout.trim() === '', 'stop must not summarise on an ordinary turn');
});

test('stop delivers the instruction when no tool call intervened', (ctx) => {
  sample(ctx, { percent: 96 });
  const r = stopHook(ctx, hookPayload(ctx));
  assertIncludes(r.stdout, 'handoff.md', 'stop should carry the instruction if the gate never ran');
});

// ── atomicity and recovery ─────────────────────────────────────────────────

test('a failed replacement write keeps the previous checkpoint', (ctx) => {
  const p = paths(ctx);
  mkdirSync(p.directory, { recursive: true });
  writeFileSync(p.checkpoint, '# good checkpoint\nthe valuable one\n');

  chmodSync(p.directory, 0o500);
  let threw = false;
  try {
    writeFileSync(join(p.directory, 'checkpoint.md.tmp.probe'), 'x');
  } catch {
    threw = true;
  }
  const after = readFileSync(p.checkpoint, 'utf8');
  chmodSync(p.directory, 0o700);

  assert(threw, 'the fixture should make writes fail');
  assertIncludes(after, 'the valuable one', 'the previous checkpoint must survive a failed write');
  assert(!readdirSync(p.directory).some((f) => f.includes('.tmp.')), 'no temp files should be left behind');
});

test('recovery preserves the checkpoint and is labelled as not written by Claude', (ctx) => {
  sample(ctx, { percent: 93 });
  const p = paths(ctx);
  mkdirSync(p.directory, { recursive: true });
  writeFileSync(p.checkpoint, '# Checkpoint\n\nThe user wants the parser rewritten in Rust.\n');

  const out = cli(ctx, ['recover', 's1', '--cwd', ctx.project]).stdout.trim();
  assert(out === p.recovery, `recover should print the recovery path, got ${out}`);
  const body = readFileSync(p.recovery, 'utf8');
  assertIncludes(body, 'not written by Claude', 'the snapshot must be labelled');
  assertIncludes(body, 'rewritten in Rust', 'the checkpoint content must be preserved');
  assertIncludes(body, 'Continue the task described below', 'the reader instruction must lead');
});

test('recovery works with no checkpoint and no git repository', (ctx) => {
  const bare = join(ctx.root, 'not-a-repo');
  mkdirSync(bare, { recursive: true });
  cli(ctx, ['recover', 'solo', '--cwd', bare]);
  const body = readFileSync(join(ctx.home, 'handoffs', 'not-a-repo', 'solo', 'recovery.md'), 'utf8');
  assertIncludes(body, 'No checkpoint was saved', 'it should say the checkpoint is missing');
  assertIncludes(body, 'Not a git repository', 'it should handle a non-repository directory');
  assertNotIncludes(body, 'undefined', 'no undefined values should reach the file');
});

test('session end writes a recovery snapshot when the bailout was never answered', (ctx) => {
  sample(ctx, { percent: 96 });
  gate(ctx, hookPayload(ctx));
  sessionEnd(ctx, { session_id: 's1', cwd: ctx.project, hook_event_name: 'SessionEnd' });

  assert(existsSync(paths(ctx).recovery), 'a recovery snapshot should exist');
  assert(state(ctx).stage === 'recovered', 'the stage should record the recovery');
});

test('session end writes nothing when the handoff was already saved', (ctx) => {
  sample(ctx, { percent: 96 });
  gate(ctx, hookPayload(ctx));
  writeFileSync(paths(ctx).handoff, '# handoff\n');
  sessionEnd(ctx, { session_id: 's1', cwd: ctx.project, hook_event_name: 'SessionEnd' });
  assert(!existsSync(paths(ctx).recovery), 'no recovery snapshot is needed when the handoff exists');
});

// ── manual use ─────────────────────────────────────────────────────────────

test('manual paths work with no telemetry at all', (ctx) => {
  const p = JSON.parse(cli(ctx, ['paths', 'fresh', '--cwd', ctx.project, '--json']).stdout);
  assertIncludes(p.handoff, 'handoff.md', 'paths should work without any sample');
  assertIncludes(p.checkpoint, 'checkpoint.md', 'paths should include the checkpoint file');
  assert(cli(ctx, ['status', 'fresh']).status === 0, 'status should not fail without telemetry');
  assertIncludes(cli(ctx, ['status', 'fresh']).stdout, 'unknown', 'status should say unknown');
});

test('simulate drives the real sampler without touching the allowance', (ctx) => {
  const out = cli(ctx, ['simulate', '--session', 'sim', '--percent', '96', '--cwd', ctx.project]).stdout;
  assertIncludes(out, 'bailout-armed', 'simulate should arm the bailout');
  assert(state(ctx, 'sim').usage.usedPercentage === 96, 'the simulated reading should be stored');
});

test('reset clears an armed stage', (ctx) => {
  sample(ctx, { percent: 96 });
  cli(ctx, ['reset', 's1', '--cwd', ctx.project]);
  assert(state(ctx).stage === 'idle', 'reset should return the session to idle');
  assert(gate(ctx, hookPayload(ctx)).stdout.trim() === '', 'nothing should be pending after a reset');
});

// ── installation ───────────────────────────────────────────────────────────

const EXISTING_STATUSLINE = `#!/bin/bash
input=$(cat)
# pre-existing third-party bridge
printf '%s' "$input" > /dev/null
echo "MY CUSTOM STATUS LINE"
`;

function installFixture(ctx) {
  const settingsPath = join(ctx.root, 'settings.json');
  const scriptPath = join(ctx.root, 'existing-statusline.sh');
  writeFileSync(scriptPath, EXISTING_STATUSLINE);
  chmodSync(scriptPath, 0o755);
  writeFileSync(
    settingsPath,
    JSON.stringify(
      {
        model: 'opus',
        statusLine: { type: 'command', command: scriptPath },
        hooks: {
          PostToolUse: [{ matcher: '*', hooks: [{ type: 'command', command: '/existing/bridge' }] }],
          Stop: [{ hooks: [{ type: 'command', command: '/existing/bridge' }] }],
        },
        permissions: { allow: ['Bash(ls:*)'] },
      },
      null,
      2,
    ),
  );
  return { settingsPath, scriptPath };
}

function statuslineOutput(scriptPath) {
  const r = spawnSync('/bin/bash', [scriptPath], { input: '{}', encoding: 'utf8' });
  return r.stdout.trim();
}

test('install merges into existing settings and preserves the status line', (ctx) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  const before = statuslineOutput(scriptPath);

  const r = run(ctx, INSTALL, ['--settings', settingsPath]);
  assert(r.status === 0, `install should succeed: ${r.stderr}`);

  const settings = JSON.parse(readFileSync(settingsPath, 'utf8'));
  assert(settings.model === 'opus', 'unrelated settings must survive');
  assert(settings.permissions.allow.length === 1, 'permissions must be untouched');
  assert(
    settings.hooks.PostToolUse.some((g) => g.hooks.some((h) => h.command === '/existing/bridge')),
    'the pre-existing PostToolUse hook must survive',
  );
  assert(
    settings.hooks.PostToolUse.some((g) => g.hooks.some((h) => h.command.includes('hooks.mjs'))),
    'the bailout gate should be registered',
  );
  assert(settings.hooks.SessionEnd, 'SessionEnd should be registered');
  assert(settings.statusLine.command === scriptPath, 'the status line command must not change');

  assertIncludes(readFileSync(scriptPath, 'utf8'), 'bailout skill sampler', 'the sampler block should be appended');
  assert(statuslineOutput(scriptPath) === before, `status line output must be unchanged, got "${statuslineOutput(scriptPath)}"`);
});

test('install is repeatable without duplicating entries', (ctx) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  run(ctx, INSTALL, ['--settings', settingsPath]);
  const afterFirst = readFileSync(scriptPath, 'utf8');
  run(ctx, INSTALL, ['--settings', settingsPath]);
  run(ctx, INSTALL, ['--settings', settingsPath]);

  const settings = JSON.parse(readFileSync(settingsPath, 'utf8'));
  const mine = settings.hooks.PostToolUse.filter((g) => g.hooks.some((h) => h.command.includes('hooks.mjs')));
  assert(mine.length === 1, `expected exactly one bailout gate group, got ${mine.length}`);
  assert(readFileSync(scriptPath, 'utf8') === afterFirst, 'the sampler block must not be appended twice');
});

test('uninstall removes only its own entries', (ctx) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  const originalScript = readFileSync(scriptPath, 'utf8');
  const before = statuslineOutput(scriptPath);

  run(ctx, INSTALL, ['--settings', settingsPath]);
  const r = run(ctx, INSTALL, ['--uninstall', '--settings', settingsPath]);
  assert(r.status === 0, `uninstall should succeed: ${r.stderr}`);

  const settings = JSON.parse(readFileSync(settingsPath, 'utf8'));
  assert(settings.model === 'opus', 'unrelated settings must survive uninstall');
  assert(settings.statusLine.command === scriptPath, 'the status line must stay configured');
  assert(
    settings.hooks.PostToolUse.every((g) => g.hooks.every((h) => !h.command.includes('hooks.mjs'))),
    'the bailout gate should be gone',
  );
  assert(
    settings.hooks.PostToolUse.some((g) => g.hooks.some((h) => h.command === '/existing/bridge')),
    'the pre-existing hook must survive uninstall',
  );
  assert(!settings.hooks.SessionEnd, 'an event that only held a bailout hook should be removed');
  assert(readFileSync(scriptPath, 'utf8') === originalScript, 'the status line script should be restored byte for byte');
  assert(statuslineOutput(scriptPath) === before, 'status line output must be restored');
});

test('uninstall restores a status line script that had no trailing newline', (ctx) => {
  const settingsPath = join(ctx.root, 'settings.json');
  const scriptPath = join(ctx.root, 'no-newline-statusline.sh');
  const original = '#!/bin/bash\ninput=$(cat)\necho "STATUS"'; // deliberately no trailing newline
  writeFileSync(scriptPath, original);
  writeFileSync(settingsPath, JSON.stringify({ statusLine: { type: 'command', command: scriptPath } }));

  run(ctx, INSTALL, ['--settings', settingsPath]);
  assertIncludes(readFileSync(scriptPath, 'utf8'), 'bailout skill sampler', 'the block should be appended');

  run(ctx, INSTALL, ['--uninstall', '--settings', settingsPath]);
  assert(readFileSync(scriptPath, 'utf8') === original, 'the missing trailing newline must be restored too');
});

test('uninstall keeps a status line the user changed afterwards', (ctx) => {
  const settingsPath = join(ctx.root, 'settings.json');
  writeFileSync(settingsPath, JSON.stringify({}));
  run(ctx, INSTALL, ['--settings', settingsPath]);

  const created = JSON.parse(readFileSync(settingsPath, 'utf8')).statusLine.command;
  assertIncludes(created, 'statusline.sh', 'a fallback status line should be created when none exists');

  // The user then points the status line somewhere else.
  const settings = JSON.parse(readFileSync(settingsPath, 'utf8'));
  settings.statusLine.command = '/my/own/statusline';
  writeFileSync(settingsPath, JSON.stringify(settings, null, 2));

  run(ctx, INSTALL, ['--uninstall', '--settings', settingsPath]);
  const after = JSON.parse(readFileSync(settingsPath, 'utf8'));
  assert(after.statusLine.command === '/my/own/statusline', 'a later user change must not be overwritten');
});

test('install backs up what it changes and dry run writes nothing', (ctx) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  const before = readFileSync(settingsPath, 'utf8');

  const dry = run(ctx, INSTALL, ['--settings', settingsPath, '--dry-run']);
  assertIncludes(dry.stdout, 'dry run', 'a dry run should say so');
  assert(readFileSync(settingsPath, 'utf8') === before, 'a dry run must not change settings');
  assert(!readFileSync(scriptPath, 'utf8').includes('bailout skill sampler'), 'a dry run must not change the script');
  assert(
    readdirSync(ctx.root).every((f) => !f.includes('.bailout-bak.')),
    'a dry run must not leave backup files either',
  );

  run(ctx, INSTALL, ['--settings', settingsPath]);
  const backups = readdirSync(ctx.root).filter((f) => f.includes('.bailout-bak.'));
  assert(backups.length >= 2, `expected backups of both files, got ${backups.join(', ')}`);
});

test('reinstall repairs a sampler block whose interpreter path has gone stale', (ctx) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  run(ctx, INSTALL, ['--settings', settingsPath]);

  // Simulate a node upgrade removing the pinned interpreter the block was
  // installed with. The sampler discards stderr, so this would fail silently.
  const stale = readFileSync(scriptPath, 'utf8').replace(
    /"[^"]*node"/,
    '"/opt/homebrew/Cellar/node/0.0.0/bin/node"',
  );
  writeFileSync(scriptPath, stale);
  assertIncludes(readFileSync(scriptPath, 'utf8'), '0.0.0', 'the fixture should be stale');

  const r = run(ctx, INSTALL, ['--settings', settingsPath]);
  assertIncludes(r.stdout, 'refreshed', 'a reinstall should report that it repaired the block');
  assertNotIncludes(readFileSync(scriptPath, 'utf8'), '0.0.0', 'the stale interpreter should be gone');
  assertIncludes(readFileSync(scriptPath, 'utf8'), 'MY CUSTOM STATUS LINE', 'the rest of the script must survive');
  assert(
    (readFileSync(scriptPath, 'utf8').match(/bailout skill sampler \(managed/g) || []).length === 1,
    'repairing must not leave two blocks behind',
  );
});

test('status reports a broken install instead of failing silently', (ctx) => {
  const { settingsPath, scriptPath } = installFixture(ctx);
  assertIncludes(cli(ctx, ['status', 's1']).stdout, 'not installed', 'an uninstalled state should say so');

  run(ctx, INSTALL, ['--settings', settingsPath]);
  assertIncludes(cli(ctx, ['status', 's1']).stdout, 'install:    ok', 'a healthy install should report ok');

  writeFileSync(
    scriptPath,
    readFileSync(scriptPath, 'utf8').replace(/"[^"]*node"/, '"/nonexistent/node"'),
  );
  const broken = cli(ctx, ['status', 's1']).stdout;
  assertIncludes(broken, 'PROBLEM', 'a missing interpreter should be reported');
  assertIncludes(broken, '/nonexistent/node', 'the report should name the missing path');
});

test('install reports rather than guesses when the status line cannot take the block', (ctx) => {
  const settingsPath = join(ctx.root, 'settings.json');
  const scriptPath = join(ctx.root, 'opaque-statusline');
  writeFileSync(scriptPath, '#!/bin/bash\necho hi\n');
  writeFileSync(settingsPath, JSON.stringify({ statusLine: { type: 'command', command: scriptPath } }));

  const r = run(ctx, INSTALL, ['--settings', settingsPath]);
  assertIncludes(r.stdout, 'manual-required', 'it should report that manual wiring is needed');
  assertIncludes(r.stdout, 'statusline', 'it should print the line to add');
  assert(readFileSync(scriptPath, 'utf8') === '#!/bin/bash\necho hi\n', 'an unrecognised script must not be edited');
});

// ── the central sequence ───────────────────────────────────────────────────

test('INTEGRATION: a continuing task crosses the threshold, hands off, and yields', (ctx) => {
  // A task is under way. Several turns pass below the thresholds; the user sends
  // nothing, and nothing is asked of Claude.
  for (const percent of [55, 70, 84]) {
    sample(ctx, { percent });
    assert(gate(ctx, hookPayload(ctx)).stdout.trim() === '', `no request expected at ${percent}%`);
    assert(stopHook(ctx, hookPayload(ctx)).stdout.trim() === '', `no stop block expected at ${percent}%`);
  }

  // The five-hour allowance crosses the checkpoint threshold mid-task.
  sample(ctx, { percent: 88 });
  const checkpointAsk = additionalContext(gate(ctx, hookPayload(ctx)));
  assertIncludes(checkpointAsk, 'checkpoint.md', 'a checkpoint should be requested');
  writeFileSync(paths(ctx).checkpoint, '# Checkpoint\n\nObjective: ship the importer.\n');

  // Ordinary work continues after a checkpoint.
  assert(stopHook(ctx, hookPayload(ctx)).stdout.trim() === '', 'a checkpoint must not stop the turn');
  sample(ctx, { percent: 90 });
  assert(gate(ctx, hookPayload(ctx)).stdout.trim() === '', 'the checkpoint must not be requested again');

  // The bailout threshold is crossed, still with no new user prompt.
  sample(ctx, { percent: 93 });
  const bailoutAsk = additionalContext(gate(ctx, hookPayload(ctx)));
  assertIncludes(bailoutAsk, 'handoff.md', 'the handoff should be requested');
  assertIncludes(bailoutAsk, 'checkpoint.md', 'the existing checkpoint should be offered to build on');
  assertIncludes(bailoutAsk, 'no new ordinary work', 'ordinary work should be told to stop');

  // Claude writes the handoff before the turn ends.
  writeFileSync(paths(ctx).handoff, '# Handoff\n\nObjective: ship the importer.\nNext: run the tests.\n');

  // The turn is allowed to end, and nothing further is asked.
  assert(stopHook(ctx, hookPayload(ctx)).stdout.trim() === '', 'the stop should be allowed');
  assert(state(ctx).stage === 'bailout-done', 'the stage should be settled');
  sample(ctx, { percent: 97 });
  assert(gate(ctx, hookPayload(ctx)).stdout.trim() === '', 'no further work should be requested after the handoff');
  assert(stopHook(ctx, hookPayload(ctx)).stdout.trim() === '', 'no further stop blocks after the handoff');

  // Session end needs no recovery snapshot, because a real handoff exists.
  sessionEnd(ctx, { session_id: 's1', cwd: ctx.project });
  assert(!existsSync(paths(ctx).recovery), 'no recovery snapshot when a real handoff was written');
});

// ── runner ─────────────────────────────────────────────────────────────────

const filter = process.argv[2];
let passed = 0;
const failures = [];

for (const { name, fn } of tests) {
  if (filter && !name.includes(filter)) continue;
  const ctx = makeContext();
  try {
    fn(ctx);
    passed += 1;
    process.stdout.write(`  ok   ${name}\n`);
  } catch (error) {
    failures.push({ name, error });
    process.stdout.write(`  FAIL ${name}\n       ${error.message.split('\n').join('\n       ')}\n`);
  } finally {
    try {
      rmSync(ctx.root, { recursive: true, force: true });
    } catch {
      // A leftover temp directory is not worth failing the run over.
    }
  }
}

process.stdout.write(`\n${passed} passed, ${failures.length} failed\n`);
process.exit(failures.length ? 1 : 0);
