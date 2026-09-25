import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { expandHome } from "./exec.mjs";
import { detectConfig } from "./init.mjs";
import { fetchAntigravity } from "./providers/antigravity.mjs";
import { fetchClaude } from "./providers/claude.mjs";
import { fetchCodex } from "./providers/codex.mjs";

const FETCHERS = { claude: fetchClaude, codex: fetchCodex, antigravity: fetchAntigravity };

export const CONFIG_PATH = expandHome(process.env.AI_USAGE_CONFIG ?? join(homedir(), ".config", "ai-usage", "config.json"));
export const CACHE_DIR = expandHome(process.env.AI_USAGE_CACHE_DIR ?? join(homedir(), ".cache", "ai-usage"));
const SNAPSHOT_PATH = join(CACHE_DIR, "snapshot.json");
const LOCK_DIR = join(CACHE_DIR, "refresh.lock");
// A live owner keeps its lock however slow the providers are; only a lock this old is presumed abandoned
// (hung owner, or its pid reused by another process).
const LOCK_MAX_AGE_MS = 10 * 60_000;

// Without a config file, monitor whatever is installed, so a fresh machine or plugin install works as-is.
export function loadConfig() {
  let raw;
  try {
    raw = readFileSync(CONFIG_PATH, "utf8");
  } catch (err) {
    if (err.code === "ENOENT") return detectConfig();
    throw err;
  }
  const config = { maxAgeSeconds: 180, env: {}, ...JSON.parse(raw) };
  const ids = new Set();
  for (const account of config.accounts) {
    if (!FETCHERS[account.provider]) throw new Error(`${CONFIG_PATH}: account "${account.id}" has unknown provider "${account.provider}"`);
    if (ids.has(account.id)) throw new Error(`${CONFIG_PATH}: duplicate account id "${account.id}"`);
    ids.add(account.id);
  }
  return config;
}

// Atomic, so an MCP server reading the config mid-write never sees half a file.
export function writeConfig(config) {
  mkdirSync(dirname(CONFIG_PATH), { recursive: true });
  const tmp = `${CONFIG_PATH}.${process.pid}.tmp`;
  writeFileSync(tmp, `${JSON.stringify(config, null, 2)}\n`);
  renameSync(tmp, CONFIG_PATH);
}

function readSnapshot() {
  try {
    return JSON.parse(readFileSync(SNAPSHOT_PATH, "utf8"));
  } catch {
    return { version: 1, accounts: {} };
  }
}

function writeSnapshot(snapshot) {
  mkdirSync(CACHE_DIR, { recursive: true });
  const tmp = `${SNAPSHOT_PATH}.${process.pid}.tmp`;
  writeFileSync(tmp, JSON.stringify(snapshot, null, 2));
  renameSync(tmp, SNAPSHOT_PATH);
}

const LOCK_OWNER = join(LOCK_DIR, "owner");
let holdingLock = false;

// Free the lock when this process exits mid-refresh (e.g. `watch` quitting). A killed process cannot
// clean up, so waiters also treat a lock whose owner is gone as stale.
process.on("exit", () => {
  if (holdingLock) unlock();
});

function ownerAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === "EPERM";
  }
}

function lockIsStale() {
  try {
    const age = Date.now() - statSync(LOCK_DIR).mtimeMs;
    if (age > LOCK_MAX_AGE_MS) return true;
    const pid = Number(readFileSync(LOCK_OWNER, "utf8"));
    return Number.isInteger(pid) && pid > 0 && !ownerAlive(pid);
  } catch (err) {
    // No owner file yet: fine for a lock created a moment ago, stale if it never gets one.
    if (err.code === "ENOENT" && existsSync(LOCK_DIR)) return Date.now() - statSync(LOCK_DIR).mtimeMs > 5_000;
    return false;
  }
}

function tryLock() {
  mkdirSync(CACHE_DIR, { recursive: true });
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      mkdirSync(LOCK_DIR);
      writeFileSync(LOCK_OWNER, String(process.pid));
      holdingLock = true;
      return true;
    } catch (err) {
      if (err.code !== "EEXIST") throw err;
      if (attempt > 0 || !lockIsStale()) return false;
      rmSync(LOCK_DIR, { recursive: true, force: true });
    }
  }
  return false;
}

// Remove the lock only if it is still ours: after a takeover, another process owns the directory.
function unlock() {
  holdingLock = false;
  try {
    if (Number(readFileSync(LOCK_OWNER, "utf8")) !== process.pid) return;
  } catch {
    return;
  }
  rmSync(LOCK_DIR, { recursive: true, force: true });
}

export const _lockInternals = { lockIsStale, tryLock, unlock, LOCK_DIR };

// Probes that are expensive for their provider are not repeated more often than this, except on an
// explicit refresh. agy starts every MCP server in its config just to answer /usage (for this user: an LSP
// via npm, a mock server under ~/Documents that makes macOS ask for Documents access, and so on), and
// Antigravity usage only moves while agy is in use. Override per account with minRefreshSeconds.
const MIN_REFRESH_SECONDS = { antigravity: 600 };

function staleIds(config, snapshot, maxAgeMs, now) {
  return config.accounts
    .filter((a) => {
      const entry = snapshot.accounts[a.id];
      const lastAttempt = Math.max(entry?.fetchedAt ?? 0, entry?.errorAt ?? 0);
      const floor = (a.minRefreshSeconds ?? MIN_REFRESH_SECONDS[a.provider] ?? 0) * 1000;
      const limit = maxAgeMs < 0 ? maxAgeMs : Math.max(maxAgeMs, floor);
      return !entry || entry.provider !== a.provider || now - lastAttempt > limit;
    })
    .map((a) => a.id);
}

async function fetchAccount(account, config, previous) {
  const started = Date.now();
  try {
    const data = await FETCHERS[account.provider]({ ...account, env: { ...config.env, ...account.env } }, { now: started });
    return { id: account.id, provider: account.provider, ok: true, fetchedAt: Date.now(), durationMs: Date.now() - started, ...data };
  } catch (err) {
    // Keep the last good reading so an outage shows stale numbers rather than nothing.
    const keep = previous?.provider === account.provider ? previous : {};
    return { ...keep, id: account.id, provider: account.provider, ok: false, error: err.message, errorAt: Date.now() };
  }
}

/**
 * Returns every configured account, refreshing any whose data is older than maxAgeSeconds.
 * One process refreshes at a time; others wait for it and then read its result.
 */
export async function getAccounts({ maxAgeSeconds, force = false, config = loadConfig() } = {}) {
  const maxAgeMs = force ? -1 : (maxAgeSeconds ?? config.maxAgeSeconds ?? 180) * 1000;
  let snapshot = readSnapshot();
  const deadline = Date.now() + 90_000;
  while (staleIds(config, snapshot, maxAgeMs, Date.now()).length > 0) {
    if (tryLock()) {
      try {
        snapshot = readSnapshot();
        const ids = staleIds(config, snapshot, maxAgeMs, Date.now());
        const accounts = config.accounts.filter((a) => ids.includes(a.id));
        const results = await Promise.all(accounts.map((a) => fetchAccount(a, config, snapshot.accounts[a.id])));
        for (const entry of results) snapshot.accounts[entry.id] = entry;
        // Drop accounts that are no longer configured instead of carrying them forever.
        for (const id of Object.keys(snapshot.accounts)) if (!config.accounts.some((a) => a.id === id)) delete snapshot.accounts[id];
        snapshot.updatedAt = Date.now();
        writeSnapshot(snapshot);
      } finally {
        unlock();
      }
      break;
    }
    // Another process is refreshing: wait for its result instead of fetching twice.
    const seen = snapshot.updatedAt;
    while (existsSync(LOCK_DIR) && !lockIsStale() && Date.now() < deadline) await sleep(300);
    snapshot = readSnapshot();
    if (Date.now() > deadline) break;
    // A forced refresh is satisfied by a refresh that completed, not by one that was abandoned.
    if (force && snapshot.updatedAt !== seen) break;
  }
  return config.accounts.map((account) => ({
    ...(snapshot.accounts[account.id] ?? { id: account.id, provider: account.provider, ok: false, error: "not fetched yet" }),
    label: account.label ?? account.id,
    short: account.short ?? account.id.slice(0, 2).toUpperCase(),
    route: account.route,
    routes: account.routes ?? {},
    billing: account.billing ?? null,
  }));
}
