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
  readFileSync(path.join(dir, "2026-10-24.jsonl"), "utf8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));

test("records fresh unchanged readings but does not duplicate an unchanged cache", () =>
  withFiles((cache, output) => {
    writeFileSync(cache, JSON.stringify(sample(NOW)));
    expect(recordSnapshot(cache, output, NOW).recorded).toBe(1);
    expect(recordSnapshot(cache, output, NOW + 60_000).recorded).toBe(0);
    writeFileSync(cache, JSON.stringify(sample(NOW + 180_000)));
    expect(recordSnapshot(cache, output, NOW + 180_000).recorded).toBe(1);
    const observations = rows(output);
    expect(observations.map((r) => [r.fetchedAt, r.windows[0].usedPct])).toEqual([
      ["2026-10-24T12:00:00.000Z", 70],
      ["2026-10-24T12:03:00.000Z", 70],
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
      retentionDays: 60,
    });
    expect(existsSync(path.join(output, "2026-01-01.jsonl"))).toBe(false);
    expect(readFileSync(path.join(output, "notes.txt"), "utf8")).toBe("keep\n");
    writeFileSync(cache, JSON.stringify(sample(NOW)));
    expect(recordSnapshot(cache, output, NOW).recorded).toBe(1);
  }));
