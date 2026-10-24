import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, test } from "vitest";

import { recordSnapshot } from "../../../plugins/monitor/bin/observe.mjs";

const NOW = Date.parse("2026-10-24T12:00:00Z");
const sample = (fetchedAt, usedPct = 70) => ({
  updatedAt: fetchedAt,
  accounts: {
    example: {
      provider: "codex",
      plan: "pro",
      ok: true,
      fetchedAt,
      windows: [
        {
          id: "weekly",
          windowMins: 10_080,
          usedPct,
          remainingPct: 100 - usedPct,
          resetsAt: NOW + 172_800_000,
        },
      ],
      resetCredits: { availableCount: 2 },
      notes: ["private-note-not-for-history"],
      token: "secret-not-for-history",
    },
  },
});
function withFiles(run) {
  const dir = mkdtempSync(path.join(tmpdir(), "monitor-observe-"));
  try {
    run(path.join(dir, "snapshot.json"), path.join(dir, "history"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}
const rows = (dir) =>
  ["2026-10-24.jsonl", "2026-10-25.jsonl"]
    .filter((name) => existsSync(path.join(dir, name)))
    .map((name) => readFileSync(path.join(dir, name), "utf8"))
    .join("")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));

test("keeps a baseline and twelve-hour checkpoints, skipping routine usage", () =>
  withFiles((cache, output) => {
    writeFileSync(cache, JSON.stringify(sample(NOW)));
    expect(recordSnapshot(cache, output, NOW).recorded).toBe(1);
    expect(recordSnapshot(cache, output, NOW + 60_000).recorded).toBe(0);
    writeFileSync(cache, JSON.stringify(sample(NOW + 180_000)));
    expect(recordSnapshot(cache, output, NOW + 180_000).recorded).toBe(0);
    writeFileSync(cache, JSON.stringify(sample(NOW + 3_600_000, 72)));
    expect(recordSnapshot(cache, output, NOW + 3_600_000).recorded).toBe(0);
    writeFileSync(cache, JSON.stringify(sample(NOW + 43_200_000, 73)));
    expect(recordSnapshot(cache, output, NOW + 43_200_000).recorded).toBe(1);
    const observations = rows(output);
    expect(observations.map((r) => [r.fetchedAt, r.windows[0].usedPct])).toEqual([
      ["2026-10-24T12:00:00.000Z", 70],
      ["2026-10-25T00:00:00.000Z", 73],
    ]);
    expect(observations[0]).toMatchObject({
      schema: "monitor.observation.v1",
      provider: "codex",
      bankedResetsAvailable: 2,
      windows: [{ reportedResetsAt: "2026-10-26T12:00:00.000Z", remainingPct: 30 }],
    });
    expect(JSON.stringify(observations)).not.toMatch(
      /secret-not-for-history|private-note-not-for-history/,
    );
  }));

test("retains an early refill and failed refresh without inventing a reset cause or a new reading", () =>
  withFiles((cache, output) => {
    writeFileSync(cache, JSON.stringify(sample(NOW)));
    recordSnapshot(cache, output, NOW);
    const refilled = sample(NOW + 180_000, 0);
    refilled.accounts.example.windows[0].resetsAt += 432_000_000;
    writeFileSync(cache, JSON.stringify(refilled));
    recordSnapshot(cache, output, NOW + 180_000);
    refilled.accounts.example.ok = false;
    refilled.accounts.example.errorAt = NOW + 360_000;
    refilled.accounts.example.error = "sensitive-provider-output";
    writeFileSync(cache, JSON.stringify(refilled));
    const status = recordSnapshot(cache, output, NOW + 360_000);
    const observations = rows(output);
    expect(observations.map((r) => [r.windows[0].remainingPct, r.refreshOk])).toEqual([
      [30, true],
      [100, true],
      [100, false],
    ]);
    expect(observations[2]).toMatchObject({
      fetchedAt: "2026-10-24T12:03:00.000Z",
      errorAt: "2026-10-24T12:06:00.000Z",
    });
    expect(status.accounts[0]).toMatchObject({ ageMinutes: 3, refreshOk: false });
    expect(JSON.stringify(observations)).not.toContain("sensitive-provider-output");
  }));

test("missing cache is visible and retention only removes old observation files", () =>
  withFiles((cache, output) => {
    mkdirSync(output);
    writeFileSync(path.join(output, "2026-01-01.jsonl"), "old\n");
    writeFileSync(path.join(output, "notes.txt"), "keep\n");
    const status = recordSnapshot(cache, output, NOW);
    expect(status).toMatchObject({
      cacheAvailable: false,
      recorded: 0,
      snapshotUpdatedAt: null,
      retentionDays: 14,
      checkpointHours: 12,
      maxRecords: 500,
    });
    expect(existsSync(path.join(output, "2026-01-01.jsonl"))).toBe(false);
    expect(readFileSync(path.join(output, "notes.txt"), "utf8")).toBe("keep\n");
    writeFileSync(cache, JSON.stringify(sample(NOW)));
    expect(recordSnapshot(cache, output, NOW).recorded).toBe(1);
  }));

test("repeated refresh failures do not create history until recovery", () =>
  withFiles((cache, output) => {
    const data = sample(NOW);
    writeFileSync(cache, JSON.stringify(data));
    recordSnapshot(cache, output, NOW);
    data.accounts.example.ok = false;
    data.accounts.example.errorAt = NOW + 60_000;
    writeFileSync(cache, JSON.stringify(data));
    expect(recordSnapshot(cache, output, NOW + 60_000).recorded).toBe(1);
    data.accounts.example.errorAt = NOW + 120_000;
    writeFileSync(cache, JSON.stringify(data));
    expect(recordSnapshot(cache, output, NOW + 120_000).recorded).toBe(0);
    data.accounts.example.ok = true;
    data.accounts.example.fetchedAt = NOW + 180_000;
    writeFileSync(cache, JSON.stringify(data));
    expect(recordSnapshot(cache, output, NOW + 180_000).recorded).toBe(1);
    expect(rows(output).map((r) => r.reasons)).toEqual([
      ["initial"],
      ["refresh_status_changed"],
      ["refresh_status_changed"],
    ]);
    expect(rows(output)[2].previous.refreshOk).toBe(false);
  }));

test("history has a hard cap even when many changes happen", () =>
  withFiles((cache, output) => {
    mkdirSync(output);
    const history =
      Array.from({ length: 501 }, (_, index) =>
        JSON.stringify({
          observationId: String(index),
          recordedAt: "2026-10-24T11:59:00.000Z",
        }),
      ).join("\n") + "\n";
    writeFileSync(path.join(output, "2026-10-24.jsonl"), history);
    recordSnapshot(cache, output, NOW);
    const kept = rows(output);
    expect(kept).toHaveLength(500);
    expect(kept[0].observationId).toBe("1");
    expect(kept.at(-1).observationId).toBe("500");
  }));

test("ordinary short-window refills do not add events", () =>
  withFiles((cache, output) => {
    const data = sample(NOW);
    data.accounts.example.windows[0].windowMins = 300;
    writeFileSync(cache, JSON.stringify(data));
    recordSnapshot(cache, output, NOW);
    data.accounts.example.fetchedAt += 180_000;
    Object.assign(data.accounts.example.windows[0], {
      usedPct: 0,
      remainingPct: 100,
      resetsAt: NOW + 18_000_000,
    });
    writeFileSync(cache, JSON.stringify(data));
    expect(recordSnapshot(cache, output, NOW + 180_000).recorded).toBe(0);
    expect(rows(output)).toHaveLength(1);
    expect(rows(output)[0].windows[0].remainingPct).toBe(30);
  }));
