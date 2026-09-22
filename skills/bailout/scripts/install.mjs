#!/usr/bin/env node
// Installer for the bailout skill.
//
//   node install.mjs [--settings PATH] [--statusline-script PATH] [--dry-run]
//   node install.mjs --uninstall [--settings PATH] [--dry-run]
//
// Rules it follows:
//   * merges only its own entries, never rewrites unrelated settings
//   * repeatable: a second run changes nothing
//   * reversible: uninstall removes its entries and leaves later edits alone
//   * backs up every file it is about to change, once per run
//   * grants no permissions and never upgrades Claude Code

import { execFileSync } from 'node:child_process';
import { chmodSync, copyFileSync, existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { bailoutHome, readJson, writeFileAtomic, writeJsonAtomic } from './lib.mjs';

const SCRIPTS_DIR = dirname(fileURLToPath(import.meta.url));
const SKILL_DIR = dirname(SCRIPTS_DIR);
const HOOKS = join(SCRIPTS_DIR, 'hooks.mjs');

// process.execPath can be version-pinned, for example
// /opt/homebrew/Cellar/node/26.9.0/bin/node. Baking that into the hooks would
// leave them broken at the next node upgrade, and the sampler discards its own
// stderr, so it would fail silently and the bailout would simply never fire.
// Prefer a stable alias that resolves to the very same binary.
function stableNodePath() {
  const candidates = [
    '/opt/homebrew/bin/node',
    '/usr/local/bin/node',
    join(homedir(), '.volta', 'bin', 'node'),
    '/usr/bin/node',
  ];
  for (const candidate of candidates) {
    if (candidate === process.execPath) return candidate;
    if (!existsSync(candidate)) continue;
    try {
      if (execFileSync(candidate, ['-v'], { encoding: 'utf8', timeout: 5000 }).trim() === process.version) {
        return candidate;
      }
    } catch {
      // Not a usable node binary; try the next candidate.
    }
  }
  return process.execPath;
}

const NODE = stableNodePath();

const MARKER = 'skills/bailout/scripts/hooks.mjs';
const BLOCK_START = '# >>> bailout skill sampler (managed block; delete to uninstall) >>>';
const BLOCK_END = '# <<< bailout skill sampler <<<';

const HOOK_EVENTS = [
  { event: 'PostToolUse', mode: 'gate', matcher: '*' },
  { event: 'Stop', mode: 'stop', matcher: null },
  { event: 'SessionEnd', mode: 'session-end', matcher: null },
];

function receiptPath() {
  return join(bailoutHome(), 'install-receipt.json');
}

function hookCommand(mode) {
  return `${JSON.stringify(NODE)} ${JSON.stringify(HOOKS)} ${mode}`;
}

// Ends with a newline and starts with none, so the caller controls the join and
// uninstall can take back exactly what was added.
function samplerBlock() {
  return [
    BLOCK_START,
    `printf '%s' "$input" | ${JSON.stringify(NODE)} ${JSON.stringify(HOOKS)} statusline 2>/dev/null || :`,
    BLOCK_END,
    '',
  ].join('\n');
}

const FALLBACK_STATUSLINE = `#!/bin/bash
# Status line created by the bailout skill. Feeds the usage sampler and prints a
# short usage summary. Safe to edit: keep the managed block below.
input=$(cat)
${BLOCK_START}
printf '%s' "$input" | ${JSON.stringify(NODE)} ${JSON.stringify(HOOKS)} statusline 2>/dev/null || :
${BLOCK_END}
printf '%s' "$input" | ${JSON.stringify(NODE)} -e '
let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{
  try{
    const s=JSON.parse(d);
    const m=s.model?.display_name||"";
    const f=s.rate_limits?.five_hour?.used_percentage;
    process.stdout.write(m+(typeof f==="number"?" \\u00b7 "+f+"% of 5h":""));
  }catch{}
});'
`;

function backup(path, changed) {
  if (changed.dryRun || !existsSync(path) || changed.backups[path]) return;
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const target = `${path}.bailout-bak.${stamp}`;
  copyFileSync(path, target);
  changed.backups[path] = target;
}

function groupHasMarker(group) {
  return (group.hooks || []).some((hook) => String(hook.command || '').includes(MARKER));
}

function installHooks(settings, changed) {
  settings.hooks = settings.hooks || {};
  for (const { event, mode, matcher } of HOOK_EVENTS) {
    const groups = Array.isArray(settings.hooks[event]) ? settings.hooks[event] : [];
    const existing = groups.find(groupHasMarker);
    const entry = { type: 'command', command: hookCommand(mode), timeout: 10 };
    if (existing) {
      // Repeatable: refresh the command in place rather than adding a second copy.
      const before = JSON.stringify(existing.hooks);
      existing.hooks = [entry];
      if (matcher) existing.matcher = matcher;
      if (before !== JSON.stringify(existing.hooks)) changed.hooksRefreshed.push(event);
    } else {
      groups.push(matcher ? { matcher, hooks: [entry] } : { hooks: [entry] });
      changed.hooksAdded.push(event);
    }
    settings.hooks[event] = groups;
  }
}

function uninstallHooks(settings, changed) {
  if (!settings.hooks) return;
  for (const { event } of HOOK_EVENTS) {
    const groups = settings.hooks[event];
    if (!Array.isArray(groups)) continue;
    const kept = groups.filter((group) => !groupHasMarker(group));
    if (kept.length !== groups.length) changed.hooksRemoved.push(event);
    if (kept.length) {
      settings.hooks[event] = kept;
    } else {
      delete settings.hooks[event];
    }
  }
  if (Object.keys(settings.hooks).length === 0) delete settings.hooks;
}

function installSampler(settings, changed, flags) {
  const explicit = flags['statusline-script'];
  const configured = typeof settings.statusLine?.command === 'string' ? settings.statusLine.command : '';
  // The configured command may carry arguments; the script is the first token.
  const configuredScript = configured.trim().split(/\s+/)[0] || '';
  const target = explicit || configuredScript;

  if (target && existsSync(target)) {
    const body = readFileSync(target, 'utf8');
    if (body.includes(BLOCK_START)) {
      // Rewrite the block when its command has drifted, for instance because the
      // node path it was installed with has since been upgraded away. Without
      // this, a reinstall could never repair a silently broken sampler.
      const blockPattern = new RegExp(
        `${escapeRegExp(BLOCK_START)}[\\s\\S]*?${escapeRegExp(BLOCK_END)}\\n?`,
      );
      const current = body.match(blockPattern);
      const desired = samplerBlock();
      if (current && current[0] === desired) {
        changed.sampler = { mode: 'already-present', path: target };
        return;
      }
      backup(target, changed);
      if (!flags['dry-run']) writeFileAtomic(target, body.replace(blockPattern, desired));
      changed.sampler = { mode: 'refreshed', path: target };
      return;
    }
    if (!/input=\$\(cat\)/.test(body)) {
      changed.sampler = { mode: 'manual-required', path: target };
      return;
    }
    backup(target, changed);
    const separator = body.endsWith('\n') ? '' : '\n';
    if (!flags['dry-run']) writeFileAtomic(target, `${body}${separator}${samplerBlock()}`);
    changed.sampler = { mode: 'appended', path: target };
    // Remembered so uninstall can restore the file exactly, including a missing
    // trailing newline.
    changed.samplerEndedWithNewline = body.endsWith('\n');
    return;
  }

  if (target && !existsSync(target)) {
    changed.sampler = { mode: 'missing-script', path: target };
    return;
  }

  // Kept in the user's bailout directory, not in the skill folder, so that
  // reinstalling or updating the skill never clobbers it.
  const generated = join(bailoutHome(), 'statusline.sh');
  if (!flags['dry-run']) {
    writeFileAtomic(generated, FALLBACK_STATUSLINE);
    chmodSync(generated, 0o755);
  }
  settings.statusLine = { type: 'command', command: generated };
  changed.sampler = { mode: 'created', path: generated };
  changed.createdStatusLine = generated;
}

function uninstallSampler(settings, changed, receipt, flags) {
  const configured = typeof settings.statusLine?.command === 'string' ? settings.statusLine.command : '';
  const configuredScript = configured.trim().split(/\s+/)[0] || '';
  const candidates = new Set(
    [flags['statusline-script'], configuredScript, receipt?.sampler?.path].filter(Boolean),
  );

  for (const path of candidates) {
    if (!existsSync(path)) continue;
    const body = readFileSync(path, 'utf8');
    if (!body.includes(BLOCK_START)) continue;
    backup(path, changed);
    let stripped = body.replace(
      new RegExp(`${escapeRegExp(BLOCK_START)}[\\s\\S]*?${escapeRegExp(BLOCK_END)}\\n?`, 'g'),
      '',
    );
    // The install added a newline only if the file lacked one. Take it back so
    // the file is restored byte for byte.
    if (receipt && receipt.samplerEndedWithNewline === false) stripped = stripped.replace(/\n+$/, '');
    if (!flags['dry-run']) writeFileAtomic(path, stripped);
    changed.sampler = { mode: 'block-removed', path };
  }

  // Only drop statusLine if this installer created the script and the setting
  // still points at it. A status line the user changed since is left alone.
  if (receipt?.createdStatusLine && configuredScript === receipt.createdStatusLine) {
    delete settings.statusLine;
    changed.sampler = { mode: 'statusline-removed', path: receipt.createdStatusLine };
  }
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function parseFlags(argv) {
  const flags = {};
  for (let i = 0; i < argv.length; i += 1) {
    if (!argv[i].startsWith('--')) continue;
    const key = argv[i].slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith('--')) {
      flags[key] = true;
    } else {
      flags[key] = next;
      i += 1;
    }
  }
  return flags;
}

function report(changed, flags, uninstalling) {
  const lines = [uninstalling ? 'bailout: uninstalled' : 'bailout: installed'];
  lines.push(`settings:  ${changed.settingsPath}`);
  if (changed.hooksAdded.length) lines.push(`hooks added:   ${changed.hooksAdded.join(', ')}`);
  if (changed.hooksRefreshed.length) lines.push(`hooks updated: ${changed.hooksRefreshed.join(', ')}`);
  if (changed.hooksRemoved.length) lines.push(`hooks removed: ${changed.hooksRemoved.join(', ')}`);
  if (changed.sampler) lines.push(`status line:   ${changed.sampler.mode} (${changed.sampler.path})`);
  for (const [path, target] of Object.entries(changed.backups)) lines.push(`backup:    ${path} -> ${target}`);
  if (changed.sampler?.mode === 'manual-required') {
    lines.push(
      '',
      'The configured status line script does not read stdin into $input, so the',
      'sampler was not added automatically. Add this line to it after it reads stdin:',
      '',
      samplerBlock().trim(),
    );
  }
  if (changed.sampler?.mode === 'missing-script') {
    lines.push('', 'The configured status line command was not found on disk; nothing was changed.');
  }
  if (flags['dry-run']) lines.push('', '(dry run: nothing was written)');
  lines.push('');
  process.stdout.write(lines.join('\n'));
}

function main() {
  const flags = parseFlags(process.argv.slice(2));
  const settingsPath = flags.settings || join(homedir(), '.claude', 'settings.json');
  const uninstalling = Boolean(flags.uninstall);
  const settings = readJson(settingsPath) || {};
  const changed = {
    settingsPath,
    hooksAdded: [],
    hooksRefreshed: [],
    hooksRemoved: [],
    backups: {},
    sampler: null,
    dryRun: Boolean(flags['dry-run']),
  };

  backup(settingsPath, changed);

  if (uninstalling) {
    const receipt = readJson(receiptPath());
    uninstallHooks(settings, changed);
    uninstallSampler(settings, changed, receipt, flags);
    if (!flags['dry-run']) {
      writeJsonAtomic(settingsPath, settings);
      writeJsonAtomic(receiptPath(), { uninstalledAt: new Date().toISOString() });
    }
  } else {
    installHooks(settings, changed);
    installSampler(settings, changed, flags);
    if (!flags['dry-run']) {
      const previous = readJson(receiptPath());
      writeJsonAtomic(settingsPath, settings);
      writeJsonAtomic(receiptPath(), {
        installedAt: new Date().toISOString(),
        skillDir: SKILL_DIR,
        settingsPath,
        node: NODE,
        sampler: changed.sampler,
        // A refresh does not re-observe the original file ending, so carry the
        // first install's answer forward or uninstall would not restore it.
        samplerEndedWithNewline:
          changed.samplerEndedWithNewline ?? previous?.samplerEndedWithNewline ?? null,
        createdStatusLine: changed.createdStatusLine || previous?.createdStatusLine || null,
      });
    }
  }

  report(changed, flags, uninstalling);
}

main();
