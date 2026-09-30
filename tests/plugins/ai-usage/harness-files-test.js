import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterAll, expect, test } from "vitest";

import { editJsonSection } from "../../../plugins/ai-usage/lib/harness-files.mjs";

const dir = mkdtempSync(path.join(tmpdir(), "ai-usage-harness-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));
const entry = { command: "/bin/ai-usage", args: ["mcp"] };

test("editJsonSection adds, is idempotent, removes, and keeps other keys and indentation", () => {
  const file = path.join(dir, "desktop.json");
  writeFileSync(file, '{\n    "preferences": {\n        "x": true\n    }\n}\n');
  expect(editJsonSection(file, "mcpServers", "ai-usage", entry)).toStrictEqual({ changed: true });
  expect(readFileSync(file, "utf8")).toMatch(/^ {4}"mcpServers": \{\n {8}"ai-usage"/m);
  expect(JSON.parse(readFileSync(file, "utf8")).preferences).toStrictEqual({ x: true });
  expect(existsSync(`${file}.bak-ai-usage`)).toBeTruthy();
  expect(editJsonSection(file, "mcpServers", "ai-usage", entry)).toStrictEqual({ changed: false });
  expect(editJsonSection(file, "mcpServers", "ai-usage", entry, { remove: true })).toStrictEqual({
    changed: true,
  });
  expect(JSON.parse(readFileSync(file, "utf8")).mcpServers).toStrictEqual({});
});

test("editJsonSection creates a missing file and refuses files with comments", () => {
  const fresh = path.join(dir, "opencode.json");
  expect(editJsonSection(fresh, "mcp", "ai-usage", entry)).toStrictEqual({ changed: true });
  expect(JSON.parse(readFileSync(fresh, "utf8")).mcp["ai-usage"]).toStrictEqual(entry);

  const commented = path.join(dir, "commented.jsonc");
  writeFileSync(commented, '{\n  // keep me\n  "a": 1\n}\n');
  expect(editJsonSection(commented, "mcp", "ai-usage", entry).manual).toMatch(/has comments/);
  expect(readFileSync(commented, "utf8")).toMatch(/keep me/);
});
