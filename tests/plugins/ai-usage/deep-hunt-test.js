import { spawn, spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterAll, expect, test } from "vitest";

// Regression tests from the deep bug hunt. Fake provider CLIs and a private cache only.
const dir = mkdtempSync(path.join(tmpdir(), "ai-usage-hunt-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));
const cli = new URL("../../../plugins/ai-usage/bin/ai-usage.mjs", import.meta.url).pathname;
const NOW = Date.parse("2026-09-25T16:37:46Z");

function script(name, body) {
  const filePath = path.join(dir, name);
  writeFileSync(filePath, body);
  chmodSync(filePath, 0o755);
  return filePath;
}

const rateLimits = `{ rateLimitsByLimitId: { codex: { limitId: "codex", planType: "pro",
  primary: { usedPercent: 25, windowDurationMins: 10080, resetsAt: Math.floor(Date.now() / 1000) + 86400 } } } }`;

// Sends a server-initiated request that reuses id 2 before answering, as JSON-RPC allows.
const chattyCodex = script(
  "codex-chatty",
  String.raw`#!/usr/bin/env node
const out = (m) => process.stdout.write(JSON.stringify(m) + "\n");
require("readline").createInterface({ input: process.stdin }).on("line", (line) => {
  const m = JSON.parse(line);
  if (m.id === 1) { out({ id: 2, method: "account/login/prompt", params: {} }); out({ id: 1, result: {} }); }
  if (m.id === 2 && m.method) out({ id: 2, result: ${rateLimits} });
});
`,
);

const okCodex = script(
  "codex-ok",
  String.raw`#!/usr/bin/env node
const out = (m) => process.stdout.write(JSON.stringify(m) + "\n");
require("readline").createInterface({ input: process.stdin }).on("line", (line) => {
  const m = JSON.parse(line);
  if (m.id === 1) out({ id: 1, result: {} });
  if (m.id === 2) out({ id: 2, result: ${rateLimits} });
});
`,
);

test("codex: a server-initiated request reusing our id is not mistaken for the response", async () => {
  const { fetchCodex } = await import("../../../plugins/ai-usage/lib/providers/codex.mjs");
  const result = await fetchCodex({ command: chattyCodex });
  expect(result.windows[0].remainingPct).toBe(75);
});

test("codex: a limit with no windows does not hide the real pool", async () => {
  const { parseCodexRateLimits } =
    await import("../../../plugins/ai-usage/lib/providers/codex.mjs");
  const { headline, sections } = await import("../../../plugins/ai-usage/lib/analyze.mjs");
  const parsed = parseCodexRateLimits({
    rateLimitsByLimitId: {
      codex: {
        limitId: "codex",
        primary: { usedPercent: 48, windowDurationMins: 10_080, resetsAt: 1_790_779_510 },
      },
      code_review: { limitId: "code_review", primary: null, secondary: null },
    },
  });
  const account = { id: "codex", short: "CX", provider: "codex", ok: true, ...parsed };
  expect(headline(account, NOW).text).toBe("52");
  expect(sections([account], NOW).sections.map((s) => [s.label, s.entries.length])).toStrictEqual([
    ["wk", 1],
  ]);
});

test("claude: /usage lines with other separators or parenthesised resets still parse", async () => {
  const { parseClaudeUsage } = await import("../../../plugins/ai-usage/lib/providers/claude.mjs");
  const r = parseClaudeUsage(
    [
      "Current session: 42% used - resets Sep 25 at 3pm (America/Toronto)",
      "Current week (all models): 11% used (resets Sep 28 at 5pm (America/Toronto))",
    ].join("\n"),
    NOW,
  );
  expect(
    r.windows.map((w) => [w.id, w.remainingPct, new Date(w.resetsAt).toISOString()]),
  ).toStrictEqual([
    ["5h", 58, "2026-09-25T19:00:00.000Z"],
    ["week", 89, "2026-09-28T21:00:00.000Z"],
  ]);
});

test("reset text: relative, tomorrow and weekday forms", async () => {
  const { parseResetText } = await import("../../../plugins/ai-usage/lib/time.mjs");
  const iso = (t) => new Date(parseResetText(t, NOW)).toISOString();
  expect(iso("in 2h 15m")).toBe(new Date(NOW + (2 * 60 + 15) * 60_000).toISOString());
  expect(iso("in 45 minutes")).toBe(new Date(NOW + 45 * 60_000).toISOString());
  expect(iso("tomorrow at 5pm (America/Toronto)")).toBe("2026-09-26T21:00:00.000Z");
  // 2026-09-25 is a Friday; the next Monday is the 28th.
  expect(iso("Monday at 5pm (America/Toronto)")).toBe("2026-09-28T21:00:00.000Z");
  expect(iso("Mon 5pm (America/Toronto)")).toBe("2026-09-28T21:00:00.000Z");
});

test("lock: a slow refresh by a live owner is not stolen, and unlock never removes someone else's lock", async () => {
  const cache = path.join(dir, "cache-lock");
  process.env.AI_USAGE_CACHE_DIR = cache;
  const store = await import(`../../../plugins/ai-usage/lib/store.mjs?lock-test`);
  const { lockIsStale, tryLock, unlock, LOCK_DIR } = store._lockInternals;
  const sleeper = spawn("sleep", ["30"]);
  try {
    mkdirSync(LOCK_DIR, { recursive: true });
    writeFileSync(path.join(LOCK_DIR, "owner"), String(sleeper.pid));
    const old = (Date.now() - 200_000) / 1000;
    utimesSync(LOCK_DIR, old, old);
    expect(lockIsStale(), "live owner, 200 s old: still working").toBe(false);
    expect(tryLock()).toBe(false);
    unlock();
    expect(
      existsSync(LOCK_DIR),
      "unlock must not remove a lock this process does not own",
    ).toBeTruthy();
    sleeper.kill();
    await new Promise((resolve) => sleeper.on("exit", resolve));
    expect(lockIsStale(), "dead owner").toBe(true);
    expect(tryLock()).toBe(true);
    unlock();
    expect(existsSync(LOCK_DIR)).toBe(false);
  } finally {
    sleeper.kill();
    delete process.env.AI_USAGE_CACHE_DIR;
  }
});

test("ranking: a nearly empty pool ranks after pools with room", async () => {
  const { buildLanes } = await import("../../../plugins/ai-usage/lib/analyze.mjs");
  const week = (pct) => [
    {
      id: "wk",
      label: "weekly",
      kind: "weekly",
      windowMins: 10_080,
      remainingPct: pct,
      usedPct: 100 - pct,
      resetsAt: NOW + 20 * 3_600_000,
    },
  ];
  const account = (id, pct) => ({
    id,
    label: id,
    provider: "codex",
    ok: true,
    fetchedAt: NOW,
    windows: week(pct),
    pools: [{ id: "p", label: "p", families: ["gpt"], windowIds: ["wk"] }],
  });
  // "almost-empty" is ahead of even pace (3% left, 20 h to go) but can barely take a task.
  const lanes = buildLanes([account("almost-empty", 3), account("roomy", 40)], NOW);
  expect(lanes.map((l) => l.accountId)).toStrictEqual(["roomy", "almost-empty"]);
  expect(lanes[1].advice).toMatch(/only 3% usable/);
});

// --- MCP server over stdio ---

const config = path.join(dir, "config.json");
writeFileSync(
  config,
  JSON.stringify({
    accounts: [{ id: "codex", provider: "codex", label: "Codex", short: "CX", command: okCodex }],
  }),
);

function mcp(lines) {
  const result = spawnSync(process.execPath, [cli, "mcp"], {
    input: lines.map((l) => (typeof l === "string" ? l : JSON.stringify(l))).join("\n") + "\n",
    encoding: "utf8",
    timeout: 30_000,
    env: {
      ...process.env,
      AI_USAGE_CONFIG: config,
      AI_USAGE_CACHE_DIR: path.join(dir, "cache-mcp"),
    },
  });
  return result.stdout
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l));
}

test("mcp: null arguments, batches, invalid messages", () => {
  const out = mcp([
    { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "recommend", arguments: null } },
    [
      { jsonrpc: "2.0", id: 2, method: "ping" },
      { jsonrpc: "2.0", id: 3, method: "tools/list" },
      { jsonrpc: "2.0", method: "notifications/initialized" },
    ],
    [],
    "42",
  ]);
  const byId = (id) => out.flat().find((m) => m.id === id);
  expect(byId(1).result.isError, byId(1).result?.content?.[0]?.text).toBe(undefined);
  const batch = out.find(Array.isArray);
  expect(
    batch?.map((m) => m.id),
    "a batch gets one array response, notifications omitted",
  ).toStrictEqual([2, 3]);
  expect(
    out.filter((m) => !Array.isArray(m) && m.id === null && m.error?.code === -32_600).length,
    "empty batch and non-object are invalid requests",
  ).toBe(2);
});

test("mcp: get_usage JSON is compact and still carries what agents route on", () => {
  const [res] = mcp([
    { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "get_usage", arguments: {} } },
  ]);
  const text = res.result.content[0].text;
  const json = JSON.parse(text.slice(text.indexOf("\n\n{") + 2));
  expect(Object.keys(json)).toStrictEqual(["schema", "generatedAt", "accounts", "lanes"]);
  expect(Object.keys(json.accounts[0]).toSorted((a, b) => a.localeCompare(b, "en"))).toStrictEqual([
    "ageMinutes",
    "billing",
    "error",
    "id",
    "label",
    "ok",
    "provider",
    "windows",
  ]);
  expect(!("pills" in json.accounts[0]) && !("sections" in json)).toBeTruthy();
  const lane = json.lanes[0];
  for (const key of [
    "accountId",
    "advice",
    "availableNowPct",
    "billing",
    "blockedUntil",
    "families",
    "label",
    "poolId",
    "route",
    "status",
    "surplusPts",
  ])
    expect(Object.hasOwn(lane, key), key).toBeTruthy();
  // The fake Codex week resets in 24 h with 75% left: expiring, so the lane says when and how fast to spend it.
  expect(lane.expiring).toBe(true);
  expect(lane.expiresAt && lane.burnPctPerHour > 0).toBeTruthy();
  expect(text).toMatch(/^Expiring soon, spend first: Codex \(75% left, resets in 2[34]h at \d{4}-/);
});

test("agy is probed at most every 10 minutes unless a refresh is forced or the account overrides it", () => {
  const counter = path.join(dir, "agy-calls");
  const fixture = new URL("fixtures/agy-usage.json", import.meta.url).pathname;
  const countingAgy = script(
    "agy-counting",
    `#!/bin/sh\necho x >> "${counter}"\ncat "${fixture}"\n`,
  );
  const calls = () =>
    existsSync(counter) ? readFileSync(counter, "utf8").trim().split("\n").length : 0;
  const run = (cfg, args) => {
    const file = path.join(dir, `config-agy-${cfg.tag}.json`);
    writeFileSync(
      file,
      JSON.stringify({
        accounts: [
          {
            id: "ag",
            provider: "antigravity",
            label: "AG",
            short: "AG",
            command: countingAgy,
            ...cfg.account,
          },
        ],
      }),
    );
    const r = spawnSync(process.execPath, [cli, ...args], {
      encoding: "utf8",
      timeout: 30_000,
      env: {
        ...process.env,
        AI_USAGE_CONFIG: file,
        AI_USAGE_CACHE_DIR: path.join(dir, `cache-agy-${cfg.tag}`),
      },
    });
    expect(r.status, r.stderr).toBe(0);
  };
  run({ tag: "default" }, ["line", "--max-age", "0"]);
  run({ tag: "default" }, ["line", "--max-age", "0"]);
  expect(calls(), "second read within 10 minutes reuses the cache").toBe(1);
  run({ tag: "default" }, ["line", "--refresh"]);
  expect(calls(), "an explicit refresh always probes").toBe(2);
  run({ tag: "override", account: { minRefreshSeconds: 0 } }, ["line", "--max-age", "0"]);
  run({ tag: "override", account: { minRefreshSeconds: 0 } }, ["line", "--max-age", "0"]);
  expect(calls(), "minRefreshSeconds: 0 removes the floor").toBe(4);
});
