import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import { editJsonSection, editZedContextServer, stripJsonc } from "../lib/harness-files.mjs";

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

test("editZedContextServer inserts into commented JSONC with matching indentation and removes cleanly", () => {
  const file = join(dir, "zed.json");
  const original = [
    "// Zed settings",
    "{",
    "    // servers",
    '    "context_servers": {',
    '        "other": {',
    '            "enabled": true,',
    "        },",
    "    },",
    '    "theme": "One Dark", // trailing comment',
    "}",
    "",
  ].join("\n");
  writeFileSync(file, original);
  assert.deepEqual(editZedContextServer(file, "ai-usage", { ...entry, env: {} }), { changed: true });
  const text = readFileSync(file, "utf8");
  assert.match(text, /^ {8}"ai-usage": \{\n {12}"command": "\/bin\/ai-usage",\n {12}"args": \["mcp"\],\n {12}"env": \{\}\n {8}\},\n {8}"other"/m);
  assert.match(text, /trailing comment/);
  assert.deepEqual(Object.keys(JSON.parse(stripJsonc(text)).context_servers), ["ai-usage", "other"]);
  assert.deepEqual(editZedContextServer(file, "ai-usage", entry), { changed: false });
  assert.deepEqual(editZedContextServer(file, "ai-usage", entry, { remove: true }), { changed: true });
  assert.equal(readFileSync(file, "utf8"), original);
});

test("editZedContextServer asks for a manual edit when there is no single anchor", () => {
  const file = join(dir, "zed-empty.json");
  writeFileSync(file, '{\n  "theme": "One Dark"\n}\n');
  assert.match(editZedContextServer(file, "ai-usage", entry).manual, /by hand/);
});
