import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterAll, expect, test } from "vitest";

// Every test here uses fake provider CLIs and a private cache, never real accounts.
const dir = mkdtempSync(path.join(tmpdir(), "ai-usage-robust-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));
const cli = new URL("../../../plugins/ai-usage/bin/ai-usage.mjs", import.meta.url).pathname;

function script(name, body) {
  const filePath = path.join(dir, name);
  writeFileSync(filePath, body);
  chmodSync(filePath, 0o755);
  return filePath;
}

// Answers the two JSON-RPC calls ai-usage makes to `codex app-server`.
const fakeCodex = script(
  "codex-ok",
  String.raw`#!/usr/bin/env node
const out = (m) => process.stdout.write(JSON.stringify(m) + "\n");
require("readline").createInterface({ input: process.stdin }).on("line", (line) => {
  const m = JSON.parse(line);
  if (m.id === 1) out({ id: 1, result: {} });
  if (m.id === 2) out({ id: 2, result: { rateLimitsByLimitId: { codex: { limitId: "codex", planType: "pro",
    primary: { usedPercent: 25, windowDurationMins: 10080, resetsAt: Math.floor(Date.now() / 1000) + 86400 } } } } });
});
`,
);

// Completes the handshake, then exits before the next request is written.
const dyingCodex = script(
  "codex-dies",
  '#!/bin/sh\nread line\necho \'{"id":1,"result":{}}\'\nexec 0<&-\nexit 0\n',
);

const config = path.join(dir, "config.json");
writeFileSync(
  config,
  JSON.stringify({
    accounts: [{ id: "codex", provider: "codex", label: "Codex", short: "CX", command: fakeCodex }],
  }),
);

function runCli(args, cache) {
  return spawnSync(process.execPath, [cli, ...args], {
    encoding: "utf8",
    timeout: 60_000,
    env: { ...process.env, AI_USAGE_CONFIG: config, AI_USAGE_CACHE_DIR: cache, NO_COLOR: "1" },
  });
}

test("a refresh lock left by a dead process is taken over instead of waited on", () => {
  const cache = path.join(dir, "cache-dead-owner");
  mkdirSync(path.join(cache, "refresh.lock"), { recursive: true });
  writeFileSync(path.join(cache, "refresh.lock", "owner"), "999999");
  const started = Date.now();
  const result = runCli(["line", "--max-age", "0"], cache);
  expect(result.status, result.stderr).toBe(0);
  // One pill per provider: 75% of the week left, rollover in under a day with capacity left, so it is expiring.
  expect(result.stdout.trim()).toMatch(/^CX 75 2[34]h⏳$/);
  expect(Date.now() - started < 15_000, "did not wait for the dead owner").toBeTruthy();
  expect(existsSync(path.join(cache, "refresh.lock")), "lock released afterwards").toBe(false);
});

test("codex dying mid-conversation is reported, not a crash", async () => {
  const { fetchCodex } = await import("../../../plugins/ai-usage/lib/providers/codex.mjs");
  for (let i = 0; i < 10; i++) {
    await expect(fetchCodex({ command: dyingCodex })).rejects.toThrow(
      /exited 0 before reporting rate limits/,
    );
  }
});

test("an agy ERROR status is retried once before it counts as a failure", async () => {
  const state = path.join(dir, "agy-state");
  const flaky = script(
    "agy-flaky",
    `#!/bin/sh
if [ ! -f "${state}" ]; then touch "${state}"; echo '{"status":"ERROR","response":"","error":"backend busy"}'; exit 0; fi
cat "${new URL("fixtures/agy-usage.json", import.meta.url).pathname}"
`,
  );
  const { fetchAntigravity } =
    await import("../../../plugins/ai-usage/lib/providers/antigravity.mjs");
  const result = await fetchAntigravity({ command: flaky });
  expect(result.pools.map((p) => p.id)).toStrictEqual(["gemini", "claude-gpt"]);
  const broken = script(
    "agy-broken",
    `#!/bin/sh\necho '{"status":"ERROR","response":"","error":"backend busy"}'\n`,
  );
  await expect(fetchAntigravity({ command: broken })).rejects.toThrow(/status ERROR: backend busy/);
});

test("bad arguments get a message and exit 2, not a stack trace or silent stale data", () => {
  const cache = path.join(dir, "cache-args");
  const unknown = runCli(["--bogus"], cache);
  expect(unknown.status).toBe(2);
  expect(unknown.stderr).toMatch(/^ai-usage: Unknown option '--bogus'/);
  const badAge = runCli(["json", "--max-age", "abc"], cache);
  expect(badAge.status).toBe(2);
  expect(badAge.stderr).toMatch(/--max-age must be a number of seconds/);
});
