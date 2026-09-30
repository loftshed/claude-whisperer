import { readFileSync } from "node:fs";
import { expect, test } from "vitest";

import {
  blockedWindows,
  buildLanes,
  durationLabel,
  headline,
  pills,
  sections,
} from "../../../plugins/ai-usage/lib/analyze.mjs";
import { parseAntigravityUsage } from "../../../plugins/ai-usage/lib/providers/antigravity.mjs";
import { parseClaudeUsage } from "../../../plugins/ai-usage/lib/providers/claude.mjs";
import { parseCodexRateLimits } from "../../../plugins/ai-usage/lib/providers/codex.mjs";
import { jsonView, pillText, renderLine } from "../../../plugins/ai-usage/lib/render.mjs";
import { parseResetText } from "../../../plugins/ai-usage/lib/time.mjs";

const fixture = (name) => readFileSync(new URL(`fixtures/${name}`, import.meta.url), "utf8");
const NOW = Date.parse("2026-09-25T16:37:46Z"); // 12:37 EDT

test("parseResetText resolves Claude's local reset text to UTC", () => {
  expect(new Date(parseResetText("Sep 25 at 8:59pm (America/Toronto)", NOW)).toISOString()).toBe(
    "2026-09-26T00:59:00.000Z",
  );
  expect(new Date(parseResetText("Sep 28 at 5pm (America/Toronto)", NOW)).toISOString()).toBe(
    "2026-09-28T21:00:00.000Z",
  );
  expect(new Date(parseResetText("Sep 28, 5pm (America/Toronto)", NOW)).toISOString()).toBe(
    "2026-09-28T21:00:00.000Z",
  );
  expect(new Date(parseResetText("3pm (America/Toronto)", NOW)).toISOString()).toBe(
    "2026-09-25T19:00:00.000Z",
  );
  expect(new Date(parseResetText("11am (America/Toronto)", NOW)).toISOString()).toBe(
    "2026-09-26T15:00:00.000Z",
  );
  expect(new Date(parseResetText("12:30am (UTC)", NOW)).toISOString()).toBe(
    "2026-09-26T00:30:00.000Z",
  );
});

test("parseResetText rolls the year over and handles DST", () => {
  const dec30 = Date.parse("2026-12-30T12:00:00Z");
  expect(new Date(parseResetText("Jan 2 at 5pm (America/Toronto)", dec30)).toISOString()).toBe(
    "2027-01-02T22:00:00.000Z",
  );
});

test("parseClaudeUsage reads the personal profile", () => {
  const r = parseClaudeUsage(fixture("claude-personal.txt"), NOW);
  expect(r.windows.map((w) => [w.id, w.remainingPct])).toStrictEqual([
    ["5h", 57],
    ["week", 89],
    ["week-fable", 100],
  ]);
  expect(new Date(r.windows[0].resetsAt).toISOString()).toBe("2026-09-25T16:50:00.000Z");
  expect(r.pools.map((p) => [p.id, p.windowIds])).toStrictEqual([
    ["all", ["5h", "week"]],
    ["fable", ["5h", "week", "week-fable"]],
  ]);
});

test("parseClaudeUsage reads an exhausted work profile with an unstarted session", () => {
  const r = parseClaudeUsage(fixture("claude-work.txt"), NOW);
  const byId = Object.fromEntries(r.windows.map((w) => [w.id, w]));
  expect(byId["5h"].remainingPct).toBe(100);
  expect(byId["5h"].resetsAt).toBe(null);
  expect(byId.week.remainingPct).toBe(0);
  expect(new Date(byId.week.resetsAt).toISOString()).toBe("2026-09-26T00:59:00.000Z");
});

test("parseClaudeUsage rejects API-billing output", () => {
  expect(() =>
    parseClaudeUsage("Total cost:            $0.0000\nTotal duration (API):  0s"),
  ).toThrow(/API billing/);
});

test("parseCodexRateLimits reads app-server rate limits", () => {
  const r = parseCodexRateLimits({
    ordinaryUsageAllowed: true,
    rateLimitsByLimitId: {
      codex: {
        limitId: "codex",
        primary: { usedPercent: 48, windowDurationMins: 10_080, resetsAt: 1_790_779_510 },
        secondary: { usedPercent: 12, windowDurationMins: 300, resetsAt: 1_790_370_000 },
        credits: { hasCredits: false, unlimited: false, balance: "0" },
        planType: "pro",
      },
    },
  });
  expect(r.plan).toBe("pro");
  expect(r.windows.map((w) => [w.id, w.remainingPct, w.windowMins])).toStrictEqual([
    ["weekly", 52, 10_080],
    ["5h", 88, 300],
  ]);
  expect(r.windows[0].resetsAt).toBe(1_790_779_510_000);
  expect(r.pools).toStrictEqual([
    { id: "codex", label: "Codex models", families: ["gpt"], windowIds: ["weekly", "5h"] },
  ]);
});

test("parseAntigravityUsage reads both quota groups", () => {
  const r = parseAntigravityUsage(JSON.parse(fixture("agy-usage.json")));
  expect(r.pools.map((p) => [p.id, p.families, p.windowIds])).toStrictEqual([
    ["gemini", ["gemini"], ["gemini-weekly", "gemini-5h"]],
    ["claude-gpt", ["claude", "gpt-oss"], ["claude-gpt-weekly", "claude-gpt-5h"]],
  ]);
  const weekly = r.windows.find((w) => w.id === "gemini-weekly");
  expect(weekly.remainingPct).toBe(6.4);
  expect(weekly.resetsAt).toBe(Date.parse("2026-09-26T00:44:52Z"));
});

function sampleAccounts() {
  const claude = (id, file) => ({
    id,
    label: id,
    provider: "claude",
    ok: true,
    fetchedAt: NOW,
    ...parseClaudeUsage(fixture(file), NOW),
  });
  return [
    claude("claude-work", "claude-work.txt"),
    claude("claude-personal", "claude-personal.txt"),
    {
      id: "antigravity",
      label: "antigravity",
      provider: "antigravity",
      ok: true,
      fetchedAt: NOW,
      ...parseAntigravityUsage(JSON.parse(fixture("agy-usage.json"))),
    },
  ];
}

test("buildLanes ranks expiring pools first, then roomy ones, then nearly empty, then blocked", () => {
  const lanes = buildLanes(sampleAccounts(), NOW).filter((l) => !l.redundant);
  // At NOW: Antigravity's Claude & GPT pool has 27.8% left and resets in ~32 h (last 20% of its week), so it is
  // expiring and goes first; personal Claude is under-used but has 3 days; Gemini has 6% usable (low room);
  // work Claude is out for the week.
  expect(lanes.map((l) => [l.accountId, l.poolId, l.status])).toStrictEqual([
    ["antigravity", "claude-gpt", "available"],
    ["claude-personal", "all", "available"],
    ["antigravity", "gemini", "available"],
    ["claude-work", "all", "blocked"],
  ]);
  const [expiring, personal, gemini, work] = lanes;
  expect(expiring.expiring).toBe(true);
  expect(expiring.advice).toMatch(
    /^use it or lose it: 27% of the weekly limit resets .* \(in 31h 4\dm\); ~0\.9%\/h uses it all$/,
  );
  expect(personal.advice).toMatch(/under-used, spend freely/);
  expect(gemini.lowRoom).toBe(true);
  expect(gemini.advice).toMatch(/only 6% usable now: small tasks only/);
  expect(new Date(work.blockedUntil).toISOString()).toBe("2026-09-26T00:59:00.000Z");
  const fable = buildLanes(sampleAccounts(), NOW).find(
    (l) => l.accountId === "claude-personal" && l.poolId === "fable",
  );
  expect(fable.subPoolOf).toBe("all");
  expect(fable.redundant, "Fable row adds nothing while the all-models limit binds").toBe(true);
});

test("a window whose reset has passed counts as full again", () => {
  const later = Date.parse("2026-09-26T01:30:00Z");
  const work = buildLanes(sampleAccounts(), later).find(
    (l) => l.accountId === "claude-work" && l.poolId === "all",
  );
  expect(work.status).toBe("available");
  expect(work.availableNowPct).toBe(100);
});

test("headline skips per-model sub-limits and shows each independent pool", () => {
  const [work, personal, ag] = sampleAccounts();
  expect(headline(work, NOW)).toStrictEqual({
    text: "0",
    level: "out",
    values: [0],
    levels: ["out"],
  });
  expect(headline(personal, NOW).text).toBe("57");
  expect(headline(ag, NOW).text).toBe("6/27");
});

test("pills show the short and weekly window separately, one pill per independent pool", () => {
  const [work, personal, ag] = sampleAccounts();
  const codex = {
    id: "codex",
    provider: "codex",
    ok: true,
    ...parseCodexRateLimits({
      rateLimitsByLimitId: {
        codex: {
          limitId: "codex",
          primary: { usedPercent: 48, windowDurationMins: 10_080, resetsAt: 1_790_779_510 },
        },
      },
    }),
  };
  const shape = (account) =>
    pills(account, NOW).map((p) => [
      p.tag,
      p.short && Math.floor(p.short.pct),
      p.weekly && Math.floor(p.weekly.pct),
    ]);
  expect(shape(work)).toStrictEqual([[null, 100, 0]]);
  expect(
    shape(personal),
    "weekly is the all-models limit; the Fable cap stays in the details",
  ).toStrictEqual([[null, 57, 89]]);
  expect(shape(codex), "Codex has only a weekly window").toStrictEqual([[null, null, 52]]);
  expect(shape(ag)).toStrictEqual([
    ["G", 100, 6],
    ["C", 100, 27],
  ]);
});

test("sections group usable pools by window length; pools used up for the week move to exhausted", () => {
  const [work, personal, ag] = sampleAccounts();
  const codex = {
    id: "codex",
    short: "CX",
    provider: "codex",
    ok: true,
    ...parseCodexRateLimits({
      rateLimitsByLimitId: {
        codex: {
          limitId: "codex",
          primary: { usedPercent: 48, windowDurationMins: 10_080, resetsAt: 1_790_779_510 },
        },
      },
    }),
  };
  // A provider on a different cycle gets its own section rather than being forced into "5h".
  const daily = {
    id: "other",
    short: "OT",
    provider: "codex",
    ok: false,
    ...parseCodexRateLimits({
      rateLimitsByLimitId: {
        codex: {
          limitId: "codex",
          primary: { usedPercent: 10, windowDurationMins: 1440, resetsAt: 1_790_400_000 },
        },
      },
    }),
  };
  const accounts = [
    { ...work, short: "CW" },
    { ...personal, short: "CP" },
    codex,
    { ...ag, short: "AG" },
    daily,
    { id: "gone", short: "GN", ok: false, error: "boom" },
  ];
  const { sections: list, exhausted, unavailable } = sections(accounts, NOW);
  const shape = list.map((s) => [
    s.label,
    s.entries.map(
      (e) => `${e.label}${e.tag ? "·" + e.tag : ""} ${Math.floor(e.pct)}${e.stale ? "!" : ""}`,
    ),
  ]);
  expect(shape).toStrictEqual([
    ["5h", ["CP 57", "AG·G 100", "AG·C 100"]],
    ["1d", ["OT 90!"]],
    ["wk", ["CP 89", "CX 52", "AG·G 6", "AG·C 27"]],
  ]);
  expect(
    exhausted.map((e) => [e.label, e.window, new Date(e.resetsAt).toISOString()]),
  ).toStrictEqual([["CW", "wk", "2026-09-26T00:59:00.000Z"]]);
  expect(unavailable).toStrictEqual([{ accountId: "gone", label: "GN", error: "boom" }]);
  expect([300, 10_080, 1440, 180, 43_200, 90, undefined].map(durationLabel)).toStrictEqual([
    "5h",
    "wk",
    "1d",
    "3h",
    "30d",
    "90m",
    "limit",
  ]);
});

test("an exhausted longer limit makes shorter ones unusable, never the other way round", () => {
  const [work, personal, ag] = sampleAccounts();
  // Work: weekly exhausted, so the 5-hour window and the Fable cap are unusable (dropdown, terminal view).
  expect(
    blockedWindows(work, NOW)
      .entries()
      .map(([id, by]) => [id, by.id])
      .toArray(),
  ).toStrictEqual([
    ["5h", "week"],
    ["week-fable", "week"],
  ]);
  expect(blockedWindows(personal, NOW).size).toBe(0);
  // 5-hour empty but the week has room: it stays in the 5h section at 0, and the week is still real.
  const sessionOut = parseClaudeUsage(
    "Current session: 100% used · resets Sep 25 at 3pm (America/Toronto)\nCurrent week (all models): 20% used · resets Sep 28 at 5pm (America/Toronto)",
    NOW,
  );
  const account = { id: "x", short: "X", provider: "claude", ok: true, ...sessionOut };
  expect(blockedWindows(account, NOW).size).toBe(0);
  const result = sections([account], NOW);
  expect(
    result.sections.map((s) => [s.label, s.entries[0].pct, s.entries[0].exhausted]),
  ).toStrictEqual([
    ["5h", 0, true],
    ["wk", 80, false],
  ]);
  expect(result.exhausted).toStrictEqual([]);
  // One Antigravity pool used up for the week, the other not: only that pool moves.
  const agSpent = structuredClone(ag);
  agSpent.windows.find((w) => w.id === "gemini-weekly").remainingPct = 0;
  const split = sections([{ ...agSpent, short: "AG" }], NOW);
  expect(split.exhausted.map((e) => `${e.label}·${e.tag}`)).toStrictEqual(["AG·G"]);
  expect(split.sections.map((s) => s.entries.map((e) => e.tag))).toStrictEqual([["C"], ["C"]]);
});

test("line: one group per provider, 5-hour · weekly, time to rollover, ☠ when the week is used up", () => {
  const [work, personal, ag] = sampleAccounts();
  const codex = {
    id: "codex",
    short: "CX",
    provider: "codex",
    ok: true,
    ...parseCodexRateLimits({
      rateLimitsByLimitId: {
        codex: {
          limitId: "codex",
          primary: { usedPercent: 48, windowDurationMins: 10_080, resetsAt: 1_790_779_510 },
        },
      },
    }),
  };
  // At NOW: work's week is gone until 00:59Z (8 h); personal has 57% of 5 h and 89% of a week ending in 3 days;
  // Codex is weekly-only; both Antigravity pools are in the last 20% of their weeks with more than 5% left.
  expect(pillText(work, NOW)).toBe("☠8h");
  expect(pillText(personal, NOW)).toBe("57·89 3d");
  expect(pillText(codex, NOW)).toBe("52 4d");
  expect(pillText(ag, NOW)).toBe("G100·6 8h⏳ C100·27 1d⏳");
  expect(pillText({ ...codex, ok: false }, NOW)).toBe("52 4d!");
  expect(
    renderLine(
      [{ ...work, short: "CW" }, { ...personal, short: "CP" }, codex, { ...ag, short: "AG" }],
      NOW,
    ),
  ).toBe("CW ☠8h · CP 57·89 3d · CX 52 4d · AG G100·6 8h⏳ C100·27 1d⏳");
});

test("jsonView exposes ISO reset times and minutes until reset", () => {
  const view = jsonView(sampleAccounts(), NOW);
  const week = view.accounts[0].windows.find((w) => w.id === "week");
  expect(week.resetsAt).toBe("2026-09-26T00:59:00.000Z");
  expect(week.resetsInMinutes).toBe(501);
});
