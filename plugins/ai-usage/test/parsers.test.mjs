import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { buildLanes, headline } from "../lib/analyze.mjs";
import { parseAntigravityUsage } from "../lib/providers/antigravity.mjs";
import { parseClaudeUsage } from "../lib/providers/claude.mjs";
import { parseCodexRateLimits } from "../lib/providers/codex.mjs";
import { jsonView } from "../lib/render.mjs";
import { parseResetText } from "../lib/time.mjs";

const fixture = (name) => readFileSync(new URL(`fixtures/${name}`, import.meta.url), "utf8");
const NOW = Date.parse("2026-09-25T16:37:46Z"); // 12:37 EDT

test("parseResetText resolves Claude's local reset text to UTC", () => {
  assert.equal(new Date(parseResetText("Sep 25 at 8:59pm (America/Toronto)", NOW)).toISOString(), "2026-09-26T00:59:00.000Z");
  assert.equal(new Date(parseResetText("Sep 28 at 5pm (America/Toronto)", NOW)).toISOString(), "2026-09-28T21:00:00.000Z");
  assert.equal(new Date(parseResetText("Sep 28, 5pm (America/Toronto)", NOW)).toISOString(), "2026-09-28T21:00:00.000Z");
  assert.equal(new Date(parseResetText("3pm (America/Toronto)", NOW)).toISOString(), "2026-09-25T19:00:00.000Z");
  assert.equal(new Date(parseResetText("11am (America/Toronto)", NOW)).toISOString(), "2026-09-26T15:00:00.000Z");
  assert.equal(new Date(parseResetText("12:30am (UTC)", NOW)).toISOString(), "2026-09-26T00:30:00.000Z");
});

test("parseResetText rolls the year over and handles DST", () => {
  const dec30 = Date.parse("2026-12-30T12:00:00Z");
  assert.equal(new Date(parseResetText("Jan 2 at 5pm (America/Toronto)", dec30)).toISOString(), "2027-01-02T22:00:00.000Z");
});

test("parseClaudeUsage reads the personal profile", () => {
  const r = parseClaudeUsage(fixture("claude-personal.txt"), NOW);
  assert.deepEqual(r.windows.map((w) => [w.id, w.remainingPct]), [["5h", 57], ["week", 89], ["week-fable", 100]]);
  assert.equal(new Date(r.windows[0].resetsAt).toISOString(), "2026-09-25T16:50:00.000Z");
  assert.deepEqual(r.pools.map((p) => [p.id, p.windowIds]), [["all", ["5h", "week"]], ["fable", ["5h", "week", "week-fable"]]]);
});

test("parseClaudeUsage reads an exhausted work profile with an unstarted session", () => {
  const r = parseClaudeUsage(fixture("claude-work.txt"), NOW);
  const byId = Object.fromEntries(r.windows.map((w) => [w.id, w]));
  assert.equal(byId["5h"].remainingPct, 100);
  assert.equal(byId["5h"].resetsAt, null);
  assert.equal(byId.week.remainingPct, 0);
  assert.equal(new Date(byId.week.resetsAt).toISOString(), "2026-09-26T00:59:00.000Z");
});

test("parseClaudeUsage rejects API-billing output", () => {
  assert.throws(() => parseClaudeUsage("Total cost:            $0.0000\nTotal duration (API):  0s"), /API billing/);
});

test("parseCodexRateLimits reads app-server rate limits", () => {
  const r = parseCodexRateLimits({
    ordinaryUsageAllowed: true,
    rateLimitsByLimitId: {
      codex: {
        limitId: "codex",
        primary: { usedPercent: 48, windowDurationMins: 10080, resetsAt: 1790779510 },
        secondary: { usedPercent: 12, windowDurationMins: 300, resetsAt: 1790370000 },
        credits: { hasCredits: false, unlimited: false, balance: "0" },
        planType: "pro",
      },
    },
  });
  assert.equal(r.plan, "pro");
  assert.deepEqual(r.windows.map((w) => [w.id, w.remainingPct, w.windowMins]), [["weekly", 52, 10080], ["5h", 88, 300]]);
  assert.equal(r.windows[0].resetsAt, 1790779510000);
  assert.deepEqual(r.pools, [{ id: "codex", label: "Codex models", families: ["gpt"], windowIds: ["weekly", "5h"] }]);
});

test("parseAntigravityUsage reads both quota groups", () => {
  const r = parseAntigravityUsage(JSON.parse(fixture("agy-usage.json")));
  assert.deepEqual(r.pools.map((p) => [p.id, p.families, p.windowIds]), [
    ["gemini", ["gemini"], ["gemini-weekly", "gemini-5h"]],
    ["claude-gpt", ["claude", "gpt"], ["claude-gpt-weekly", "claude-gpt-5h"]],
  ]);
  const weekly = r.windows.find((w) => w.id === "gemini-weekly");
  assert.equal(weekly.remainingPct, 6.4);
  assert.equal(weekly.resetsAt, Date.parse("2026-09-26T00:44:52Z"));
});

function sampleAccounts() {
  const claude = (id, file) => ({ id, label: id, provider: "claude", ok: true, fetchedAt: NOW, ...parseClaudeUsage(fixture(file), NOW) });
  return [
    claude("claude-work", "claude-work.txt"),
    claude("claude-personal", "claude-personal.txt"),
    { id: "antigravity", label: "antigravity", provider: "antigravity", ok: true, fetchedAt: NOW, ...parseAntigravityUsage(JSON.parse(fixture("agy-usage.json"))) },
  ];
}

test("buildLanes ranks under-used pools first and exhausted ones last", () => {
  const lanes = buildLanes(sampleAccounts(), NOW);
  assert.equal(lanes[0].accountId, "claude-personal");
  assert.equal(lanes.at(-1).accountId, "claude-work");
  assert.equal(lanes.at(-1).status, "blocked");
  assert.equal(new Date(lanes.at(-1).blockedUntil).toISOString(), "2026-09-26T00:59:00.000Z");
  const gemini = lanes.find((l) => l.poolId === "gemini");
  assert.match(gemini.advice, /on pace/);
  const fable = lanes.find((l) => l.accountId === "claude-personal" && l.poolId === "fable");
  assert.equal(fable.subPoolOf, "all");
  assert.equal(fable.redundant, true, "Fable row adds nothing while the all-models limit binds");
});

test("a window whose reset has passed counts as full again", () => {
  const later = Date.parse("2026-09-26T01:30:00Z");
  const work = buildLanes(sampleAccounts(), later).find((l) => l.accountId === "claude-work" && l.poolId === "all");
  assert.equal(work.status, "available");
  assert.equal(work.availableNowPct, 100);
});

test("headline skips per-model sub-limits and shows each independent pool", () => {
  const [work, personal, ag] = sampleAccounts();
  assert.deepEqual(headline(work, NOW), { text: "0", level: "out", values: [0], levels: ["out"] });
  assert.equal(headline(personal, NOW).text, "57");
  assert.equal(headline(ag, NOW).text, "6/27");
});

test("jsonView exposes ISO reset times and minutes until reset", () => {
  const view = jsonView(sampleAccounts(), NOW);
  const week = view.accounts[0].windows.find((w) => w.id === "week");
  assert.equal(week.resetsAt, "2026-09-26T00:59:00.000Z");
  assert.equal(week.resetsInMinutes, 501);
});
