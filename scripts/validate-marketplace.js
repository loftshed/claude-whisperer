#!/usr/bin/env node
// Checks the marketplace as a whole and reports every problem it finds in one run:
// the manifest's shape and order, that each plugin's version agrees wherever it is
// written down, skill frontmatter, and that ${CLAUDE_PLUGIN_ROOT}/${CLAUDE_SKILL_DIR}
// paths point at real files.

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

export const REPO_ROOT = path.resolve(import.meta.dirname, "..");

// Claude Code cuts a skill's description plus when_to_use at this length in the
// listing the model chooses skills from, so anything past it is never seen.
export const DESCRIPTION_BUDGET_CHARS = 1536;

const SEMVER = /^\d+\.\d+\.\d+(?:-[\w.]+)?$/u;
const TEXT_EXTENSIONS = new Set([".json", ".md", ".mjs", ".js", ".py", ".sh"]);

function readJson(filePath, context) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    context.errors.push(
      `${relative(context, filePath)}: ${error.code === "ENOENT" ? "missing" : error.message}`,
    );
    return null;
  }
}

function relative(context, filePath) {
  return path.relative(context.root, filePath);
}

function listDirectories(directory) {
  if (!fs.existsSync(directory)) return [];
  return fs
    .readdirSync(directory, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && !entry.name.startsWith("."))
    .map((entry) => entry.name)
    .toSorted((left, right) => left.localeCompare(right));
}

function listFiles(directory) {
  const files = [];
  const entries = fs.readdirSync(directory, { recursive: true, withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    const filePath = path.join(entry.parentPath, entry.name);
    if (filePath.includes(`${path.sep}node_modules${path.sep}`)) continue;
    files.push(filePath);
  }
  return files;
}

/** Top-level `key: value` pairs of a Markdown file's YAML frontmatter, plus `metadata.version`. */
export function parseFrontmatter(text) {
  const lines = text.split(/\r?\n/u);
  if (lines[0]?.trimEnd() !== "---") return { error: "missing frontmatter", fields: {} };
  const end = lines.indexOf("---", 1);
  if (end === -1) return { error: "unterminated frontmatter", fields: {} };

  const fields = {};
  let section = "";
  for (const line of lines.slice(1, end)) {
    if (!line.trim() || line.trimStart().startsWith("#")) continue;
    const match = /^(\s*)([\w-]+):\s*(.*)$/u.exec(line);
    if (!match) continue;
    const [, indent, key, rawValue] = match;
    const value = rawValue.replace(/^(["'])(.*)\1$/u, "$2");
    if (indent) {
      if (section === "metadata" && key === "version") fields["metadata.version"] = value;
      continue;
    }
    section = key;
    fields[key] = value;
  }
  return { error: null, fields };
}

/** Version rows of the README plugin table: `| name or [name](link) | 1.2.3 | ... |`. */
export function readmeVersions(text) {
  const versions = new Map();
  for (const match of text.matchAll(
    /^\|\s*\[?`([\w-]+)`\]?(?:\([^)]*\))?\s*\|\s*([^|\s]+)\s*\|/gmu,
  )) {
    versions.set(match[1], match[2]);
  }
  return versions;
}

/** The first `## x.y.z` heading of a changelog, ignoring an `## Unreleased` section. */
export function latestChangelogVersion(text) {
  for (const match of text.matchAll(/^##\s+\[?([^\]\s]+)\]?/gmu)) {
    if (match[1].toLowerCase() !== "unreleased") return match[1];
  }
  return null;
}

export function checkManifest(manifest, pluginDirectories) {
  const errors = [];
  if (!manifest.name) errors.push("marketplace.json: missing `name`");
  if (!manifest.owner?.name) errors.push("marketplace.json: missing `owner.name`");
  const plugins = Array.isArray(manifest.plugins) ? manifest.plugins : [];
  if (plugins.length === 0) errors.push("marketplace.json: `plugins` must be a non-empty array");

  const names = plugins.map((plugin) => plugin?.name);
  const sorted = names.toSorted((left, right) => left.localeCompare(right));
  if (names.join("\n") !== sorted.join("\n")) {
    errors.push(`marketplace.json: sort \`plugins\` by name; current order: ${names.join(", ")}`);
  }
  const seen = new Set();
  for (const plugin of plugins) {
    if (!plugin?.name || !plugin.source || !plugin.description) {
      errors.push(`marketplace.json: every plugin needs name, source and description`);
      continue;
    }
    if (seen.has(plugin.name)) errors.push(`marketplace.json: duplicate plugin ${plugin.name}`);
    seen.add(plugin.name);
    if (plugin.source !== `./plugins/${plugin.name}`) {
      errors.push(`marketplace.json: ${plugin.name} source should be ./plugins/${plugin.name}`);
    }
  }
  for (const directory of pluginDirectories) {
    if (!seen.has(directory)) errors.push(`plugins/${directory} is not listed in marketplace.json`);
  }
  return errors;
}

function checkSkill(skillDirectory, context) {
  const { errors } = context;
  const skillFile = path.join(skillDirectory, "SKILL.md");
  if (!fs.existsSync(skillFile)) {
    errors.push(`${relative(context, skillDirectory)}: missing SKILL.md`);
    return {};
  }
  const { error, fields } = parseFrontmatter(fs.readFileSync(skillFile, "utf8"));
  const label = relative(context, skillFile);
  if (error) {
    errors.push(`${label}: ${error}`);
    return {};
  }
  const expected = path.basename(skillDirectory);
  if (fields.name !== expected) errors.push(`${label}: name should be ${expected}`);
  if (!fields.description) errors.push(`${label}: missing description`);
  const budget = (fields.description ?? "").length + (fields.when_to_use ?? "").length;
  if (budget > DESCRIPTION_BUDGET_CHARS) {
    errors.push(
      `${label}: description is ${budget} chars; the budget is ${DESCRIPTION_BUDGET_CHARS}`,
    );
  }
  return fields;
}

function checkPathReferences(pluginDirectory, context) {
  const pattern = /\$\{(CLAUDE_PLUGIN_ROOT|CLAUDE_SKILL_DIR)\}\/([\w./-]+)/gu;
  for (const filePath of listFiles(pluginDirectory)) {
    if (!TEXT_EXTENSIONS.has(path.extname(filePath))) continue;
    const text = fs.readFileSync(filePath, "utf8");
    const [top, skill] = path.relative(pluginDirectory, filePath).split(path.sep);
    const references = text.matchAll(pattern);
    for (const [, variable, reference] of references) {
      let base = pluginDirectory;
      if (variable === "CLAUDE_SKILL_DIR") {
        if (top !== "skills" || !skill) continue;
        base = path.join(pluginDirectory, "skills", skill);
      }
      const target = reference.replace(/[.,]+$/u, "");
      if (!fs.existsSync(path.join(base, target))) {
        context.errors.push(
          `${relative(context, filePath)}: \${${variable}}/${target} does not exist`,
        );
      }
    }
  }
}

export function validate(root = REPO_ROOT) {
  const errors = [];
  const context = { errors, root };
  const pluginsRoot = path.join(root, "plugins");
  const manifest = readJson(path.join(root, ".claude-plugin", "marketplace.json"), context);
  if (!manifest) return errors;
  errors.push(...checkManifest(manifest, listDirectories(pluginsRoot)));

  const readme = fs.readFileSync(path.join(root, "README.md"), "utf8");
  const listed = readmeVersions(readme);

  const plugins = manifest.plugins ?? [];
  for (const { name } of plugins) {
    const pluginDirectory = path.join(pluginsRoot, name ?? "");
    if (!name || !fs.existsSync(pluginDirectory)) {
      errors.push(`marketplace.json: plugins/${name} does not exist`);
      continue;
    }
    const pluginJson = readJson(
      path.join(pluginDirectory, ".claude-plugin", "plugin.json"),
      context,
    );
    if (!pluginJson) continue;
    const { version } = pluginJson;
    if (pluginJson.name !== name)
      errors.push(`plugins/${name}: plugin.json name is ${pluginJson.name}`);
    if (!SEMVER.test(version ?? ""))
      errors.push(`plugins/${name}: plugin.json version ${version} is not semver`);

    const sources = new Map([["README.md", listed.get(name)]]);
    const packagePath = path.join(pluginDirectory, "package.json");
    if (fs.existsSync(packagePath))
      sources.set("package.json", readJson(packagePath, context)?.version);
    const changelogPath = path.join(pluginDirectory, "CHANGELOG.md");
    if (fs.existsSync(changelogPath)) {
      sources.set("CHANGELOG.md", latestChangelogVersion(fs.readFileSync(changelogPath, "utf8")));
    }
    const skills = listDirectories(path.join(pluginDirectory, "skills"));
    for (const skill of skills) {
      const fields = checkSkill(path.join(pluginDirectory, "skills", skill), context);
      if (fields["metadata.version"]) {
        sources.set(`skills/${skill}/SKILL.md metadata.version`, fields["metadata.version"]);
      }
    }
    for (const [source, found] of sources) {
      if (found !== version) {
        errors.push(
          `plugins/${name}: ${source} says ${found ?? "nothing"}, plugin.json says ${version}`,
        );
      }
    }
    checkPathReferences(pluginDirectory, context);
  }

  const looseSkills = listDirectories(path.join(root, "skills"));
  for (const skill of looseSkills) {
    checkSkill(path.join(root, "skills", skill), context);
  }
  return errors;
}

function main() {
  const errors = validate();
  if (errors.length > 0) {
    console.error("marketplace validation failed:");
    for (const error of errors) console.error(`  - ${error}`);
    process.exitCode = 1;
    return;
  }
  console.log("marketplace validation ok.");
}

// Compare real paths: the script may be started through a symlink.
if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) main();
