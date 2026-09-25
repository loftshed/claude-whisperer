import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { editJsonSection } from "../lib/harness-files.mjs";

const dir = mkdtempSync(join(tmpdir(), "ai-usage-harness-"));
after(() => rmSync(dir, { recursive: true, force: true }));
const entry = { command: "/bin/ai-usage", args: ["mcp"] };

test("editJsonSection adds, is idempotent, removes, and keeps other keys and indentation", () => {
  const file = join(dir, "desktop.json");
  writeFileSync(file, '{\n    "preferences": {\n        "x": true\n    }\n}\n');
  assert.deepEqual(editJsonSection(file, "mcpServers", "ai-usage", entry), { changed: true });
  assert.match(readFileSync(file, "utf8"), /^ {4}"mcpServers": \{\n {8}"ai-usage"/m);
  assert.deepEqual(JSON.parse(readFileSync(file, "utf8")).preferences, { x: true });
  assert.ok(existsSync(`${file}.bak-ai-usage`));
  assert.deepEqual(editJsonSection(file, "mcpServers", "ai-usage", entry), { changed: false });
  assert.deepEqual(editJsonSection(file, "mcpServers", "ai-usage", entry, { remove: true }), { changed: true });
  assert.deepEqual(JSON.parse(readFileSync(file, "utf8")).mcpServers, {});
});

test("editJsonSection creates a missing file and refuses files with comments", () => {
  const fresh = join(dir, "opencode.json");
  assert.deepEqual(editJsonSection(fresh, "mcp", "ai-usage", entry), { changed: true });
  assert.deepEqual(JSON.parse(readFileSync(fresh, "utf8")).mcp["ai-usage"], entry);

  const commented = join(dir, "commented.jsonc");
  writeFileSync(commented, '{\n  // keep me\n  "a": 1\n}\n');
  assert.match(editJsonSection(commented, "mcp", "ai-usage", entry).manual, /has comments/);
  assert.match(readFileSync(commented, "utf8"), /keep me/);
});
