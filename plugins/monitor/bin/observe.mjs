#!/usr/bin/env node
// Passive recorder: reads Monitor's existing cache, never queries a provider or changes quota.
import { createHash } from "node:crypto";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

const DAY = 86_400_000;
const RETENTION_DAYS = 60;
const number = (value) => (Number.isFinite(value) ? value : null);
const timestamp = (value) =>
  Number.isFinite(value) && Math.abs(value) < 8.64e15 ? new Date(value).toISOString() : null;

function readJson(file, fallback) {
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return fallback;
    throw error;
  }
}

function writeJson(file, value) {
  const temp = `${file}.${process.pid}.tmp`;
  writeFileSync(temp, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 });
  renameSync(temp, file);
}

/** Store fresh provider readings, including unchanged percentages and failed refresh attempts. */
export function recordSnapshot(cacheFile, outputDir, now = Date.now()) {
  const snapshot = readJson(cacheFile, null);
  mkdirSync(outputDir, { recursive: true, mode: 0o700 });
  const statePath = path.join(outputDir, "state.json");
  const state = readJson(statePath, { accounts: {} });
  const today = new Date(now).toISOString().slice(0, 10);
  let recorded = 0;
  const accounts = snapshot?.accounts ?? {};
  for (const [id, account] of Object.entries(accounts)) {
    const sample = {
      accountId: id,
      provider: account.provider ?? "unknown",
      plan: account.plan ?? null,
      fetchedAt: timestamp(account.fetchedAt),
      refreshOk: account.ok !== false,
      errorAt: timestamp(account.errorAt),
      windows: (account.windows ?? []).map((w) => ({
        id: w.id,
        windowMins: number(w.windowMins),
        usedPct: number(w.usedPct),
        remainingPct: number(w.remainingPct),
        reportedResetsAt: timestamp(w.resetsAt),
      })),
      bankedResetsAvailable: number(account.resetCredits?.availableCount),
    };
    // Includes fetchedAt: a fresh reading with unchanged usage is evidence too. Identical cached reads aren't.
    const observationId = createHash("sha256").update(JSON.stringify(sample)).digest("hex");
    if (state.accounts[id] === observationId) continue;
    appendFileSync(
      path.join(outputDir, `${today}.jsonl`),
      JSON.stringify({
        schema: "monitor.observation.v1",
        observationId,
        recordedAt: new Date(now).toISOString(),
        ...sample,
      }) + "\n",
      { mode: 0o600 },
    );
    state.accounts[id] = observationId;
    recorded++;
  }
  // A crash before this checkpoint may repeat a row; observationId lets analysis discard duplicates.
  writeJson(statePath, state);
  const cutoff = new Date(now - RETENTION_DAYS * DAY).toISOString().slice(0, 10);
  for (const name of readdirSync(outputDir)) {
    if (/^\d{4}-\d{2}-\d{2}\.jsonl$/u.test(name) && name.slice(0, 10) < cutoff)
      rmSync(path.join(outputDir, name));
  }
  const status = {
    schema: "monitor.observer-status.v1",
    checkedAt: new Date(now).toISOString(),
    cacheAvailable: snapshot !== null,
    snapshotUpdatedAt: timestamp(snapshot?.updatedAt),
    recorded,
    retentionDays: RETENTION_DAYS,
    accounts: Object.entries(accounts).map(([id, a]) => ({
      accountId: id,
      fetchedAt: timestamp(a.fetchedAt),
      ageMinutes: Number.isFinite(a.fetchedAt)
        ? Math.max(0, Math.round((now - a.fetchedAt) / 60_000))
        : null,
      refreshOk: a.ok !== false,
    })),
  };
  writeJson(path.join(outputDir, "status.json"), status);
  return status;
}

if (
  process.argv[1] &&
  existsSync(process.argv[1]) &&
  realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))
) {
  const { values } = parseArgs({
    options: {
      "cache-dir": {
        type: "string",
        default: process.env.MONITOR_CACHE_DIR ?? path.join(homedir(), ".cache", "monitor"),
      },
      "output-dir": {
        type: "string",
        default: path.join(homedir(), ".local", "share", "monitor-observations"),
      },
    },
  });
  try {
    recordSnapshot(path.join(values["cache-dir"], "snapshot.json"), values["output-dir"]);
  } catch (error) {
    console.error(`monitor observer: ${error.code ?? error.name}; no provider was queried`);
    process.exitCode = 1;
  }
}
