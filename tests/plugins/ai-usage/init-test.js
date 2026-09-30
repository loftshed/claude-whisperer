import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterAll, expect, test } from "vitest";

import { detectConfig } from "../../../plugins/ai-usage/lib/init.mjs";

const home = mkdtempSync(path.join(tmpdir(), "ai-usage-home-"));
afterAll(() => rmSync(home, { recursive: true, force: true }));

test("detectConfig finds the default and extra Claude profiles plus installed CLIs", () => {
  mkdirSync(path.join(home, ".claude"));
  mkdirSync(path.join(home, ".claude-personal", "projects"), { recursive: true });
  mkdirSync(path.join(home, ".claude-history-viewer"));
  writeFileSync(path.join(home, ".claude-history-viewer", "cache.json"), "{}");

  const config = detectConfig({
    home,
    binaries: () => true,
    env: { NODE_EXTRA_CA_CERTS: path.join(home, "corp.pem") },
  });
  expect(config.accounts.map((a) => [a.id, a.short, a.configDir ?? null])).toStrictEqual([
    ["claude", "CD", null],
    ["claude-personal", "CP", "~/.claude-personal"],
    ["codex", "CX", null],
    ["antigravity", "AG", null],
  ]);
  expect(config.env).toStrictEqual({ NODE_EXTRA_CA_CERTS: "~/corp.pem" });
});

test("detectConfig leaves out CLIs that are not installed", () => {
  const config = detectConfig({ home, binaries: (name) => name === "codex", env: {} });
  expect(config.accounts.map((a) => a.id)).toStrictEqual(["codex"]);
  expect(config.env).toStrictEqual({});
});
