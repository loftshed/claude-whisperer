import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";

// Every test here uses fake provider CLIs and a private cache, never real accounts.
const dir = mkdtempSync(join(tmpdir(), "ai-usage-robust-"));
after(() => rmSync(dir, { recursive: true, force: true }));
const cli = new URL("../bin/ai-usage.mjs", import.meta.url).pathname;

function script(name, body) {
  const path = join(dir, name);
  writeFileSync(path, body);
  chmodSync(path, 0o755);
  return path;
}

// Answers the two JSON-RPC calls ai-usage makes to `codex app-server`.
const fakeCodex = script(
  "codex-ok",
  `#!/usr/bin/env node
const out = (m) => process.stdout.write(JSON.stringify(m) + "\\n");
require("readline").createInterface({ input: process.stdin }).on("line", (line) => {
  const m = JSON.parse(line);
  if (m.id === 1) out({ id: 1, result: {} });
  if (m.id === 2) out({ id: 2, result: { rateLimitsByLimitId: { codex: { limitId: "codex", planType: "pro",
    primary: { usedPercent: 25, windowDurationMins: 10080, resetsAt: Math.floor(Date.now() / 1000) + 86400 } } } } });
});
`,
);

// Completes the handshake, then exits before the next request is written.
const dyingCodex = script("codex-dies", "#!/bin/sh\nread line\necho '{\"id\":1,\"result\":{}}'\nexec 0<&-\nexit 0\n");

const config = join(dir, "config.json");
writeFileSync(config, JSON.stringify({ accounts: [{ id: "codex", provider: "codex", label: "Codex", short: "CX", command: fakeCodex }] }));

function runCli(args, cache) {
  return spawnSync(process.execPath, [cli, ...args], {
    encoding: "utf8",
    timeout: 60_000,
    env: { ...process.env, AI_USAGE_CONFIG: config, AI_USAGE_CACHE_DIR: cache, NO_COLOR: "1" },
  });
}

test("a refresh lock left by a dead process is taken over instead of waited on", () => {
  const cache = join(dir, "cache-dead-owner");
  mkdirSync(join(cache, "refresh.lock"), { recursive: true });
  writeFileSync(join(cache, "refresh.lock", "owner"), "999999");
  const started = Date.now();
  const result = runCli(["line", "--max-age", "0"], cache);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), "wk CX 75");
  assert.ok(Date.now() - started < 15_000, "did not wait for the dead owner");
  assert.equal(existsSync(join(cache, "refresh.lock")), false, "lock released afterwards");
});

test("codex dying mid-conversation is reported, not a crash", async () => {
  const { fetchCodex } = await import("../lib/providers/codex.mjs");
  for (let i = 0; i < 10; i++) {
    await assert.rejects(fetchCodex({ command: dyingCodex }), /exited 0 before reporting rate limits/);
  }
});

test("an agy ERROR status is retried once before it counts as a failure", async () => {
  const state = join(dir, "agy-state");
  const flaky = script("agy-flaky", `#!/bin/sh
if [ ! -f "${state}" ]; then touch "${state}"; echo '{"status":"ERROR","response":"","error":"backend busy"}'; exit 0; fi
cat "${new URL("fixtures/agy-usage.json", import.meta.url).pathname}"
`);
  const { fetchAntigravity } = await import("../lib/providers/antigravity.mjs");
  const result = await fetchAntigravity({ command: flaky });
  assert.deepEqual(result.pools.map((p) => p.id), ["gemini", "claude-gpt"]);
  const broken = script("agy-broken", `#!/bin/sh\necho '{"status":"ERROR","response":"","error":"backend busy"}'\n`);
  await assert.rejects(fetchAntigravity({ command: broken }), /status ERROR: backend busy/);
});

test("bad arguments get a message and exit 2, not a stack trace or silent stale data", () => {
  const cache = join(dir, "cache-args");
  const unknown = runCli(["--bogus"], cache);
  assert.equal(unknown.status, 2);
  assert.match(unknown.stderr, /^ai-usage: Unknown option '--bogus'/);
  const badAge = runCli(["json", "--max-age", "abc"], cache);
  assert.equal(badAge.status, 2);
  assert.match(badAge.stderr, /--max-age must be a number of seconds/);
});
