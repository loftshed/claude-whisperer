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
const RETENTION_DAYS = 14;
const CHECKPOINT_MS = 12 * 60 * 60_000;
const MAX_RECORDS = 500;

function reasonsFor(sample, previous, lastRecordedAt, now) {
  if (!previous) return ["initial"];
  const reasons = [];
  if (sample.refreshOk !== previous.refreshOk) reasons.push("refresh_status_changed");
  if (sample.plan !== previous.plan || sample.provider !== previous.provider)
    reasons.push("plan_changed");
  if (sample.bankedResetsAvailable !== previous.bankedResetsAvailable)
    reasons.push("banked_resets_changed");
  const windowSet = (windows) =>
    windows
      .map((w) => `${w.id}:${w.windowMins}`)
      .sort((a, b) => a.localeCompare(b))
      .join(",");
  if (windowSet(sample.windows) !== windowSet(previous.windows))
    reasons.push("quota_windows_changed");
  for (const window of sample.windows) {
    if (window.windowMins < 1440) continue;
    const before = previous.windows.find((w) => w.id === window.id);
    if (!before) continue;
    const oldReset = before.reportedResetsAt;
    const newReset = window.reportedResetsAt;
    if (
      Boolean(oldReset) !== Boolean(newReset) ||
      (oldReset && newReset && Math.abs(Date.parse(newReset) - Date.parse(oldReset)) > 30 * 60_000)
    )
      reasons.push("reported_reset_changed");
    if (window.remainingPct === null || before.remainingPct === null) continue;
    const change = window.remainingPct - before.remainingPct;
    if (change >= 5) reasons.push("allowance_increased");
    if (change <= -25) reasons.push("large_usage_change");
  }
  if (
    reasons.length === 0 &&
    sample.refreshOk &&
    sample.fetchedAt !== previous.fetchedAt &&
    now - lastRecordedAt >= CHECKPOINT_MS
  )
    reasons.push("checkpoint");
  return [...new Set(reasons)];
}

function pruneObservations(outputDir, now) {
  const cutoff = new Date(now - RETENTION_DAYS * DAY).toISOString();
  const files = readdirSync(outputDir)
    .filter((name) => /^\d{4}-\d{2}-\d{2}\.jsonl$/u.test(name))
    .sort((a, b) => a.localeCompare(b));
  const entries = [];
  for (const name of files) {
    if (name.slice(0, 10) < cutoff.slice(0, 10)) {
      rmSync(path.join(outputDir, name));
      continue;
    }
    const content = readFileSync(path.join(outputDir, name), "utf8");
    const lines = content
      .trim()
      .split("\n")
      .filter(Boolean)
      .filter((line) => JSON.parse(line).recordedAt >= cutoff);
    entries.push({ name, content, lines });
  }
  let remaining = MAX_RECORDS;
  for (const entry of entries.toReversed()) {
    const keep = remaining > 0 ? entry.lines.slice(-remaining) : [];
    remaining -= keep.length;
    const file = path.join(outputDir, entry.name);
    if (keep.length === 0) rmSync(file);
    else if (entry.content !== keep.join("\n") + "\n") {
      const temp = `${file}.${process.pid}.tmp`;
      writeFileSync(temp, keep.join("\n") + "\n", { mode: 0o600 });
      renameSync(temp, file);
    }
  }
}
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

/** Keep sparse checkpoints and notable changes, without inferring a reset cause. */
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
    const observationId = createHash("sha256").update(JSON.stringify(sample)).digest("hex");
    const previous = state.accounts[id];
    if (previous?.observationId === observationId) continue;
    const reasons = reasonsFor(sample, previous?.lastSeen, previous?.lastRecordedAt ?? 0, now);
    if (reasons.length > 0) {
      const isEvent = reasons.some((reason) => !["initial", "checkpoint"].includes(reason));
      appendFileSync(
        path.join(outputDir, `${today}.jsonl`),
        JSON.stringify({
          schema: "monitor.observation.v1",
          observationId,
          recordedAt: new Date(now).toISOString(),
          reasons,
          ...sample,
          ...(isEvent && { previous: previous.lastSeen }),
        }) + "\n",
        { mode: 0o600 },
      );
      recorded++;
    }
    // Keep just the latest reading in place, even when it does not warrant a history entry.
    state.accounts[id] = {
      observationId,
      lastSeen: sample,
      lastRecordedAt: reasons.length > 0 ? now : previous.lastRecordedAt,
    };
  }
  writeJson(statePath, state);
  pruneObservations(outputDir, now);
  const status = {
    schema: "monitor.observer-status.v1",
    checkedAt: new Date(now).toISOString(),
    cacheAvailable: snapshot !== null,
    snapshotUpdatedAt: timestamp(snapshot?.updatedAt),
    recorded,
    retentionDays: RETENTION_DAYS,
    checkpointHours: CHECKPOINT_MS / 3_600_000,
    maxRecords: MAX_RECORDS,
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
