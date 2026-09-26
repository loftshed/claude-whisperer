// Shared state, config and threshold logic for the bailout skill.
// No dependencies outside Node core. Every path is overridable via env so the
// tests never touch the real ~/.claude/bailout directory.

import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync, unlinkSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, basename } from 'node:path';

export const SCHEMA = 1;

export const DEFAULT_CONFIG = {
  enabled: true,
  // Percent of the five-hour subscription allowance consumed.
  bailoutPercent: 95,
  checkpointPercent: 90,
  checkpointEnabled: true,
  // Heavier models burn the five-hour allowance faster, so a single sample can
  // jump further before the next one lands. Their threshold is pulled this many
  // points earlier. Keys are matched as substrings of the model id.
  modelMarginPercent: {
    opus: 3,
    fable: 3,
    sonnet: 1,
    haiku: 0,
    default: 1,
  },
  // A sample older than this is reported as unknown rather than as a number.
  staleAfterSeconds: 900,
  // How many times the Stop hook may ask again when the handoff is still missing.
  maxStopBlocks: 2,
};

export function bailoutHome() {
  return process.env.BAILOUT_HOME || join(homedir(), '.claude', 'bailout');
}

export function configPath() {
  return process.env.BAILOUT_CONFIG || join(bailoutHome(), 'config.json');
}

export function loadConfig() {
  const config = { ...DEFAULT_CONFIG, modelMarginPercent: { ...DEFAULT_CONFIG.modelMarginPercent } };
  const onDisk = readJson(configPath());
  if (!onDisk) return config;
  for (const [key, value] of Object.entries(onDisk)) {
    if (key === 'modelMarginPercent' && value && typeof value === 'object') {
      Object.assign(config.modelMarginPercent, value);
    } else if (key in DEFAULT_CONFIG) {
      config[key] = value;
    }
  }
  return config;
}

export function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    return null;
  }
}

// Writes via a temp file and rename. A failed or partial write leaves whatever
// was already on disk untouched, so the last good checkpoint always survives.
export function writeFileAtomic(path, contents) {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.tmp.${process.pid}.${Math.random().toString(36).slice(2, 8)}`;
  try {
    writeFileSync(tmp, contents);
    renameSync(tmp, path);
  } catch (error) {
    try {
      if (existsSync(tmp)) unlinkSync(tmp);
    } catch {
      // Leaving a temp file behind is not worth failing the caller over.
    }
    throw error;
  }
}

export function writeJsonAtomic(path, value) {
  writeFileAtomic(path, `${JSON.stringify(value, null, 2)}\n`);
}

export function slugify(value) {
  const slug = String(value || '')
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60);
  return slug || 'unknown';
}

export function sessionStatePath(sessionId) {
  return join(bailoutHome(), 'sessions', `${slugify(sessionId)}.json`);
}

export function projectSlug(cwd) {
  return slugify(basename(cwd || '') || 'no-project');
}

export function shortSession(sessionId) {
  return slugify(sessionId).slice(0, 8) || 'session';
}

export function handoffDir(cwd, sessionId) {
  return join(bailoutHome(), 'handoffs', projectSlug(cwd), shortSession(sessionId));
}

export const HANDOFF_FILES = {
  checkpoint: 'checkpoint.md',
  bailout: 'handoff.md',
  recovery: 'recovery.md',
};

export function handoffPath(cwd, sessionId, kind) {
  return join(handoffDir(cwd, sessionId), HANDOFF_FILES[kind] || HANDOFF_FILES.bailout);
}

// The margin is subtracted, so a heavier model bails out earlier.
export function marginForModel(config, modelId) {
  const id = String(modelId || '').toLowerCase();
  const margins = config.modelMarginPercent || {};
  for (const key of Object.keys(margins)) {
    if (key !== 'default' && id.includes(key.toLowerCase())) return Number(margins[key]) || 0;
  }
  return Number(margins.default) || 0;
}

export function effectiveThresholds(config, modelId) {
  const margin = marginForModel(config, modelId);
  const clamp = (n) => Math.max(1, Math.min(100, n));
  return {
    margin,
    bailout: clamp(config.bailoutPercent - margin),
    checkpoint: clamp(config.checkpointPercent - margin),
  };
}

export function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

export function readState(sessionId) {
  return readJson(sessionStatePath(sessionId));
}

export function writeState(state) {
  writeJsonAtomic(sessionStatePath(state.sessionId), state);
}

export function newState(sessionId) {
  return {
    schema: SCHEMA,
    sessionId,
    stage: 'idle',
    pending: null,
    window: null,
    usage: null,
    // When each kind of handoff was last asked for, so a file from an earlier
    // request cannot be mistaken for an answer to this one.
    armed: {},
    handoffs: {},
    stopBlocks: 0,
  };
}

// A five-hour sample is only usable if it carries a numeric percentage.
export function readFiveHour(statuslineInput) {
  const window = statuslineInput?.rate_limits?.five_hour;
  if (!window || typeof window.used_percentage !== 'number' || Number.isNaN(window.used_percentage)) {
    return null;
  }
  return {
    usedPercentage: window.used_percentage,
    resetsAt: typeof window.resets_at === 'number' ? window.resets_at : null,
  };
}

export function isStale(state, config, now = nowSeconds()) {
  const sampledAt = state?.usage?.sampledAt;
  if (typeof sampledAt !== 'number') return true;
  return now - sampledAt > config.staleAfterSeconds;
}

export function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    if (process.stdin.isTTY) {
      resolve('');
      return;
    }
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => {
      data += chunk;
    });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', () => resolve(data));
  });
}

export function parseJsonSafe(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export function fileExists(path) {
  try {
    return existsSync(path);
  } catch {
    return false;
  }
}
