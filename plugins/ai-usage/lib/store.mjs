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
const LOCK_STALE_MS = 120_000;

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

export function writeConfig(config) {
  mkdirSync(dirname(CONFIG_PATH), { recursive: true });
  writeFileSync(CONFIG_PATH, `${JSON.stringify(config, null, 2)}\n`);
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

function tryLock() {
  mkdirSync(CACHE_DIR, { recursive: true });
  try {
    mkdirSync(LOCK_DIR);
    return true;
  } catch (err) {
    if (err.code !== "EEXIST") throw err;
    try {
      if (Date.now() - statSync(LOCK_DIR).mtimeMs > LOCK_STALE_MS) {
        rmSync(LOCK_DIR, { recursive: true, force: true });
        mkdirSync(LOCK_DIR);
        return true;
      }
    } catch {}
    return false;
  }
}

const unlock = () => rmSync(LOCK_DIR, { recursive: true, force: true });

function staleIds(config, snapshot, maxAgeMs, now) {
  return config.accounts
    .filter((a) => {
      const entry = snapshot.accounts[a.id];
      const lastAttempt = Math.max(entry?.fetchedAt ?? 0, entry?.errorAt ?? 0);
      return !entry || entry.provider !== a.provider || now - lastAttempt > maxAgeMs;
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
        snapshot.updatedAt = Date.now();
        writeSnapshot(snapshot);
      } finally {
        unlock();
      }
      break;
    }
    // Another process is refreshing: wait for its result instead of fetching twice.
    while (existsSync(LOCK_DIR) && Date.now() < deadline) await sleep(300);
    snapshot = readSnapshot();
    if (force || Date.now() > deadline) break;
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
