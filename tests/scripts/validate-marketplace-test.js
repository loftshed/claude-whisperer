import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { expect, test } from "vitest";

import {
  checkManifest,
  latestChangelogVersion,
  parseFrontmatter,
  readmeVersions,
  validate,
} from "../../scripts/validate-marketplace.js";

function write(root, file, contents) {
  const target = path.join(root, file);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, typeof contents === "string" ? contents : JSON.stringify(contents));
}

function fixtureRepo({ readmeVersion = "1.0.0", skillName = "demo" } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "validate-marketplace-"));
  write(root, ".claude-plugin/marketplace.json", {
    name: "fixture",
    owner: { name: "tester" },
    plugins: [{ name: "demo", source: "./plugins/demo", description: "Demo." }],
  });
  write(root, "plugins/demo/.claude-plugin/plugin.json", { name: "demo", version: "1.0.0" });
  write(root, "plugins/demo/CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n## 1.0.0\n");
  write(
    root,
    "plugins/demo/skills/demo/SKILL.md",
    `---\nname: ${skillName}\ndescription: Does a demo.\n---\n\nRun \`node \${CLAUDE_SKILL_DIR}/run.mjs\`.\n`,
  );
  write(root, "plugins/demo/skills/demo/run.mjs", "");
  write(
    root,
    "README.md",
    `| Plugin | Version |\n| --- | --- |\n| \`demo\` | ${readmeVersion} |\n`,
  );
  return root;
}

test("a consistent repository validates cleanly", () => {
  expect(validate(fixtureRepo())).toStrictEqual([]);
});

test("a README version that drifted from plugin.json is reported", () => {
  expect(validate(fixtureRepo({ readmeVersion: "0.9.0" }))).toStrictEqual([
    "plugins/demo: README.md says 0.9.0, plugin.json says 1.0.0",
  ]);
});

test("a skill whose name differs from its directory is reported", () => {
  const errors = validate(fixtureRepo({ skillName: "other" }));
  expect(errors.length).toBe(1);
  expect(errors[0]).toMatch(/SKILL\.md: name should be demo/u);
});

test("a missing plugin-root reference is reported", () => {
  const root = fixtureRepo();
  fs.rmSync(path.join(root, "plugins/demo/skills/demo/run.mjs"));
  expect(validate(root)).toStrictEqual([
    "plugins/demo/skills/demo/SKILL.md: ${CLAUDE_SKILL_DIR}/run.mjs does not exist",
  ]);
});

test("the manifest must be sorted and list every plugin directory", () => {
  const manifest = {
    name: "fixture",
    owner: { name: "tester" },
    plugins: [
      { name: "b", source: "./plugins/b", description: "B." },
      { name: "a", source: "./plugins/a", description: "A." },
    ],
  };
  expect(checkManifest(manifest, ["a", "b", "c"])).toStrictEqual([
    "marketplace.json: sort `plugins` by name; current order: b, a",
    "plugins/c is not listed in marketplace.json",
  ]);
});

test("frontmatter parsing reads top-level keys and metadata.version", () => {
  const { error, fields } = parseFrontmatter(
    '---\nname: x\ndescription: "quoted"\nmetadata:\n  version: 1.2.3\n---\nbody\n',
  );
  expect(error).toBe(null);
  expect(fields).toStrictEqual({
    description: "quoted",
    metadata: "",
    "metadata.version": "1.2.3",
    name: "x",
  });
  expect(parseFrontmatter("no frontmatter").error).toBe("missing frontmatter");
});

test("README rows with and without links are read", () => {
  const versions = readmeVersions(
    "| `a` | 1.0.0 | x |\n| [`b`](plugins/b/README.md)   | 2.0.0   | y |\n",
  );
  expect([...versions]).toStrictEqual([
    ["a", "1.0.0"],
    ["b", "2.0.0"],
  ]);
});

test("the latest changelog version skips Unreleased", () => {
  expect(latestChangelogVersion("## Unreleased\n\n## 0.9.0 (2026-09-29)\n## 0.8.0\n")).toBe(
    "0.9.0",
  );
  expect(latestChangelogVersion("# Changelog\n")).toBe(null);
});
