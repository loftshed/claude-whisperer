import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { detectConfig } from "../lib/init.mjs";

const home = mkdtempSync(join(tmpdir(), "ai-usage-home-"));
after(() => rmSync(home, { recursive: true, force: true }));

test("detectConfig finds the default and extra Claude profiles plus installed CLIs", () => {
  mkdirSync(join(home, ".claude"));
  mkdirSync(join(home, ".claude-personal", "projects"), { recursive: true });
  mkdirSync(join(home, ".claude-history-viewer"));
  writeFileSync(join(home, ".claude-history-viewer", "cache.json"), "{}");

  const config = detectConfig({ home, binaries: () => true, env: { NODE_EXTRA_CA_CERTS: join(home, "corp.pem") } });
  assert.deepEqual(
    config.accounts.map((a) => [a.id, a.short, a.configDir ?? null]),
    [
      ["claude", "CD", null],
      ["claude-personal", "CP", "~/.claude-personal"],
      ["codex", "CX", null],
      ["antigravity", "AG", null],
    ],
  );
  assert.deepEqual(config.env, { NODE_EXTRA_CA_CERTS: "~/corp.pem" });
});

test("detectConfig leaves out CLIs that are not installed", () => {
  const config = detectConfig({ home, binaries: (name) => name === "codex", env: {} });
  assert.deepEqual(config.accounts.map((a) => a.id), ["codex"]);
  assert.deepEqual(config.env, {});
});
