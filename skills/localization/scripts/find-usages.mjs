#!/usr/bin/env node
// Find where translation keys are consumed in the repo's source code and print
// each usage site with surrounding lines, so element type (button, heading,
// toast, …) is judged from the real render site instead of guessed from the key
// path. Repo-aware via the same adapter as the rest of the skill; the catalog's
// own translations dir is excluded so locale files don't count as usages.
// Read-only — it reports locations, it never edits.
//
//   node find-usages.mjs <key> [<key> ...] [--path <repo>] [--catalog <id>] [--context <n>] [--verbose] [--json]
//
//   --context n    lines of code shown around each hit (default 3)
//   --verbose      print every ranked hit with source context
//   --json         machine-readable result
//
// A key with no hits usually means the reference is built dynamically
// (string concatenation, key maps) — fall back to grepping a distinctive
// fragment of the key by hand.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { detectRepo, resolveCatalog } from "./lib/adapters.mjs";
import { isTestPath, parseGitGrep, rankUsages } from "./lib/usage.mjs";

// Applies one shifted-off argument to `options` (mutated in place), consuming
// its value from `rest` when it takes one. Kept as its own function so the
// per-argument `switch` isn't nested inside the `parseArgs` loop.
function applyArg(a, rest, options) {
  switch (a) {
    case "--json": {
      options.json = true;
      break;
    }
    case "--verbose": {
      options.verbose = true;
      break;
    }
    case "--path": {
      options.path = rest.shift();
      break;
    }
    case "--catalog": {
      options.catalog = rest.shift();
      break;
    }
    case "--context": {
      options.context = Number(rest.shift());
      break;
    }
    default: {
      if (!a.startsWith("--")) options.keys.push(a);
    }
  }
}

export function parseArgs(argv) {
  const options = { json: false, verbose: false, path: null, catalog: null, context: 3, keys: [] };
  const rest = [...argv];
  while (rest.length > 0) {
    applyArg(rest.shift(), rest, options);
  }
  return options;
}

const VENDORED_EXCLUDES = [
  ":(exclude).yarn",
  ":(exclude)node_modules",
  ":(exclude)vendor",
  ":(exclude)dist",
  ":(exclude)build",
  ":(exclude).cache",
];

function untrackedSources(context) {
  const result = spawnSync(
    "git",
    [
      "ls-files",
      "--others",
      "--exclude-standard",
      "-z",
      "--",
      ".",
      ...(context.catalogDirs ?? [context.dir]).map((dir) => `:(exclude,literal)${dir}`),
      ...VENDORED_EXCLUDES,
    ],
    { cwd: context.root, encoding: "utf8" },
  );
  if (result.error) throw new Error(`git ls-files failed: ${result.error.message}`);
  if (result.status !== 0) {
    throw new Error(result.stderr.trim() || `git ls-files exited ${result.status}`);
  }

  const sources = [];
  const files = result.stdout.split("\0").filter(Boolean);
  for (const file of files) {
    try {
      const source = fs.readFileSync(path.join(context.root, file), "utf8");
      if (!source.includes("\0")) sources.push({ file, source });
    } catch {
      // A racing deletion or unreadable untracked file cannot be a stable usage.
    }
  }
  return sources;
}

function sourceHits(file, source, needle) {
  const hits = [];
  for (const [index, text] of source.split("\n").entries()) {
    if (text.includes(needle)) hits.push({ file, line: index + 1, text });
  }
  return hits;
}

// All tracked and untracked working-tree hits for a fixed string (the
// repository's catalog dirs excluded so locale files never count). `git grep` exits 1
// on "no matches", which is a valid empty result, not an error.
function grepFixed(context, needle) {
  const result = spawnSync(
    "git",
    [
      "grep",
      "-n",
      "--fixed-strings",
      needle,
      "--",
      ".",
      ...(context.catalogDirs ?? [context.dir]).map((dir) => `:(exclude,literal)${dir}`),
      ...VENDORED_EXCLUDES,
    ],
    { cwd: context.root, encoding: "utf8" },
  );
  if (result.error) throw new Error(`git grep failed: ${result.error.message}`);
  if (result.status !== 0 && result.status !== 1) {
    throw new Error(result.stderr.trim() || `git grep exited ${result.status}`);
  }
  return [
    ...parseGitGrep(result.stdout),
    ...(context.untrackedSources ?? []).flatMap(({ file, source }) =>
      sourceHits(file, source, needle),
    ),
  ];
}

export function findDefineMessagesProperty(source, key) {
  const escapedKey = key.replaceAll(/[$()*+./?[\\\]^{|}]/g, String.raw`\$&`);
  const idPattern = new RegExp(String.raw`\bid\s*:\s*["']${escapedKey}["']`);
  const propertyPattern = /^\s*([a-zA-Z_$][\w$]*)\s*:\s*\{/;
  const lines = source.split("\n");
  let activeDefineMessagesLine = -1;

  for (const [lineIndex, line] of lines.entries()) {
    if (/\bdefineMessages\s*\(/.test(line)) {
      activeDefineMessagesLine = lineIndex;
    }
    const idMatch = idPattern.exec(line);
    if (idMatch && activeDefineMessagesLine >= 0) {
      const sameLine = line.slice(0, idMatch.index).match(/([a-zA-Z_$][\w$]*)\s*:\s*\{[^{}]*$/);
      if (sameLine) return sameLine[1];

      // Real defineMessages definitions put `defaultMessage` between the property
      // opening and `id`. The nearest property-shaped line inside the active
      // defineMessages call is its member.
      for (let candidate = lineIndex - 1; candidate > activeDefineMessagesLine; candidate -= 1) {
        const match = propertyPattern.exec(lines[candidate]);
        if (match) return match[1];
      }
    }
    if (activeDefineMessagesLine >= 0 && /^\s*\}\s*\)\s*;?/.test(line)) {
      activeDefineMessagesLine = -1;
    }
  }
  return null;
}

export function resolveDefineMessagesProperty(
  definitionHits,
  key,
  { root = process.cwd(), readFileSync = fs.readFileSync } = {},
) {
  const definitionFiles = new Set(definitionHits.map((hit) => hit.file));
  for (const file of definitionFiles) {
    try {
      const property = findDefineMessagesProperty(readFileSync(path.join(root, file), "utf8"), key);
      if (property) return { file, property };
    } catch {
      // A missing/racing definition file only disables indirection for this
      // key; the literal-key hits are still useful.
    }
  }
  return null;
}

function moduleStem(file) {
  return file.replace(/\.(?:[cm]?[jt]sx?)$/i, "").replace(/[/\\]index$/i, "");
}

function importTargetsDefinition(specifier, consumerFile, definitionFile, root) {
  if (!specifier.startsWith(".")) return false;
  const candidate = path.resolve(root, path.dirname(consumerFile), specifier);
  const definition = path.resolve(root, definitionFile);
  return moduleStem(candidate) === moduleStem(definition);
}

// A binding clause cannot start with a quoted side-effect import or cross a
// statement boundary while looking for its `from` clause.
const IMPORT_FROM = /\bimport\s+([a-zA-Z_$*{][^;]*?)\s+from\s+["']([^"']+)["']/g;
const MODULE_EXTENSIONS = ["", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"];

// Local bindings that directly import the defineMessages module. Restricting
// member hits to these bindings prevents a common property such as `.title`
// from matching unrelated objects across the repository.
export function importBindingsForDefinition(source, consumerFile, definitionFile, root) {
  const bindings = new Set();
  for (const match of source.matchAll(IMPORT_FROM)) {
    if (!importTargetsDefinition(match[2], consumerFile, definitionFile, root)) continue;
    const clause = match[1].trim();
    const defaultBinding = /^([a-zA-Z_$][\w$]*)/.exec(clause);
    if (defaultBinding) bindings.add(defaultBinding[1]);
    const namespaceBinding = /\*\s+as\s+([a-zA-Z_$][\w$]*)/.exec(clause);
    if (namespaceBinding) bindings.add(namespaceBinding[1]);
    const named = /\{([^}]*)\}/.exec(clause);
    const namedItems = named?.[1].split(",") ?? [];
    for (const item of namedItems) {
      const local = /(?:^|\s+as\s+)([a-zA-Z_$][\w$]*)\s*$/.exec(item.trim());
      if (local) bindings.add(local[1]);
    }
  }

  const requirePattern =
    /\b(?:const|let|var)\s+([a-zA-Z_$][\w$]*)\s*=\s*require\(\s*["']([^"']+)["']\s*\)/g;
  for (const match of source.matchAll(requirePattern)) {
    if (importTargetsDefinition(match[2], consumerFile, definitionFile, root)) {
      bindings.add(match[1]);
    }
  }
  return bindings;
}

function relativeImports(source) {
  const specifiers = new Set();
  const requires = /\brequire\(\s*["']([^"']+)["']\s*\)/g;
  for (const [pattern, group] of [
    [IMPORT_FROM, 2],
    [requires, 1],
  ]) {
    for (const match of source.matchAll(pattern)) {
      if (match[group].startsWith(".")) specifiers.add(match[group]);
    }
  }
  return specifiers;
}

function importedDefinitionFiles(source, file, root) {
  const files = new Set();
  for (const specifier of relativeImports(source)) {
    const base = path.resolve(root, path.dirname(file), specifier);
    const candidates = [base, path.join(base, "index")].flatMap((candidate) =>
      MODULE_EXTENSIONS.map((extension) => candidate + extension),
    );
    const definition = candidates.find((candidate) =>
      fs.statSync(candidate, { throwIfNoEntry: false })?.isFile(),
    );
    if (definition) files.add(path.relative(root, definition));
  }
  return files;
}

function bindingsUseProperty(text, bindings, property) {
  const escape = (value) => value.replaceAll(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`);
  const escapedProperty = escape(property);
  return bindings
    .values()
    .some((binding) =>
      new RegExp(
        String.raw`(?:^|[^\w$])${escape(binding)}\s*\.\s*${escapedProperty}(?![\w$])`,
      ).test(text),
    );
}

function definedMessageProperties(definition, sourceValues) {
  const properties = new Map();
  if (!/\bdefineMessages\s*\(/.test(definition)) return properties;
  for (const key of sourceValues.keys()) {
    if (!definition.includes(key)) continue;
    const property = findDefineMessagesProperty(definition, key);
    if (property) properties.set(key, property);
  }
  return properties;
}

// Branch diffs often add only `messages.title`: the full catalog id remains in
// an unchanged definition module. Resolve each changed file's own imports and
// inspect only its added lines, so common property names in unrelated modules
// and unchanged render sites cannot widen discovery.
export function newlyUsedMessageKeys(context, sourceValues, additions) {
  const used = new Set();
  const definitions = new Map();
  for (const addition of additions) {
    if (!addition.addedText.includes(".")) continue;
    const source =
      addition.source ?? fs.readFileSync(path.join(context.root, addition.file), "utf8");
    for (const file of importedDefinitionFiles(source, addition.file, context.root)) {
      if (!definitions.has(file)) {
        const definition = fs.readFileSync(path.join(context.root, file), "utf8");
        definitions.set(file, definedMessageProperties(definition, sourceValues));
      }
      const bindings = importBindingsForDefinition(source, addition.file, file, context.root);
      for (const [key, property] of definitions.get(file)) {
        if (bindingsUseProperty(addition.addedText, bindings, property)) {
          used.add(key);
        }
      }
    }
  }
  return used;
}

function scopedMemberHits(context, resolution) {
  const cache = new Map();
  return grepFixed(context, `.${resolution.property}`).filter((hit) => {
    let source = cache.get(hit.file);
    if (source === undefined) {
      try {
        source = fs.readFileSync(path.join(context.root, hit.file), "utf8");
      } catch {
        source = null;
      }
      cache.set(hit.file, source);
    }
    if (source === null) return false;
    const bindings = importBindingsForDefinition(source, hit.file, resolution.file, context.root);
    return bindingsUseProperty(hit.text, bindings, resolution.property);
  });
}

// The hit's surrounding lines, read from the working tree. Fails soft to null —
// the file:line hit is still worth reporting when the context read hiccups.
function readContext(root, file, line, context) {
  try {
    const lines = fs.readFileSync(path.join(root, file), "utf8").split("\n");
    const start = Math.max(1, line - context);
    const end = Math.min(lines.length, line + context);
    return { startLine: start, lines: lines.slice(start - 1, end) };
  } catch {
    return null;
  }
}

export function run(options) {
  if (options.keys.length === 0) return { fatal: "no key given (e.g. app.generic.close)" };
  if (!Number.isSafeInteger(options.context) || options.context < 0) {
    return { fatal: `--context must be a non-negative integer` };
  }

  const repo = detectRepo(options.path || process.cwd());
  if (repo.repo === "unknown") return { fatal: `not a known i18n repo: ${repo.reason}` };
  const context = resolveCatalog(repo, options.catalog);
  if (context.error) return { fatal: context.error };
  context.untrackedSources = untrackedSources(context);

  const results = options.keys.map((key) => {
    const keyHits = grepFixed(context, key);
    let alias = null;
    let hits = keyHits;
    if (context.messageIndirection) {
      const resolution = resolveDefineMessagesProperty(keyHits, key, { root: context.root });
      if (resolution) {
        alias = resolution.property;
        hits = [...keyHits, ...scopedMemberHits(context, resolution)];
      }
    }
    // Opt-in per key: only a caller that has established the key is a plural may
    // accept a runtime-built final segment as a reference to it.
    const dynamicPrefix = options.dynamicPrefixKeys?.has(key) ?? false;
    const { shown, omitted } = rankUsages(hits, key, { alias, dynamicPrefix });
    return {
      key,
      omitted,
      usages: shown.map((h) => ({
        ...h,
        test: isTestPath(h.file),
        context: readContext(context.root, h.file, h.line, options.context),
      })),
    };
  });

  const missing = results.filter((r) => r.usages.length === 0).map((r) => r.key);
  return { repo: context.repo, catalog: context.id, results, missing };
}

export function classifyUsageResult(result) {
  const production = result.usages.filter((usage) => !usage.test && !usage.definition);
  if (production.length > 0) return { status: "rendered", usage: production[0] };
  const definitions = result.usages.filter((usage) => usage.definition);
  if (definitions.length > 0) return { status: "definition-only", usage: definitions[0] };
  const test = result.usages.filter((usage) => usage.test && !usage.definition);
  if (test.length > 0) return { status: "test-only", usage: test[0] };
  return { status: "unreferenced", usage: null };
}

function formatSummary(report) {
  const classified = report.results.map((result) => ({
    key: result.key,
    ...classifyUsageResult(result),
  }));
  const counts = new Map();
  for (const result of classified) {
    counts.set(result.status, (counts.get(result.status) ?? 0) + 1);
  }
  const ordered = ["rendered", "definition-only", "test-only", "unreferenced"];
  const totals = ordered
    .filter((status) => counts.has(status))
    .map((status) => `${counts.get(status)} ${status}`)
    .join(", ");
  const lines = [`find-usages: ${classified.length} key(s): ${totals}`];
  for (const result of classified) {
    const location = result.usage ? ` → ${result.usage.file}:${result.usage.line}` : "";
    lines.push(`  [${result.status}] ${result.key}${location}`);
  }
  return lines.join("\n");
}

function formatVerbose(report) {
  if (report.fatal) return `find-usages: ${report.fatal}`;

  const lines = [];
  for (const r of report.results) {
    lines.push(`## ${r.key}`);
    if (r.usages.length === 0) {
      lines.push(
        "  no usage site found — the reference may be built dynamically; grep a fragment of the key by hand.",
      );
      lines.push("");
      continue;
    }
    for (const u of r.usages) {
      const tags = [
        u.test ? "test file" : null,
        u.definition ? "definition, not a render site" : null,
      ]
        .filter(Boolean)
        .join(", ");
      lines.push(`${u.file}:${u.line}${tags ? `  (${tags})` : ""}`);
      if (u.context) {
        for (const [index, text] of u.context.lines.entries()) {
          const lineNumber = u.context.startLine + index;
          const marker = lineNumber === u.line ? " →" : "  ";
          lines.push(`${marker} ${String(lineNumber).padStart(4)} | ${text}`);
        }
      }
      lines.push("");
    }
    if (r.omitted > 0) lines.push(`  (+${r.omitted} more usage site(s) not shown)`, "");
  }
  return lines.join("\n").trimEnd();
}

export function formatHuman(report, { verbose = false } = {}) {
  if (report.fatal) return `find-usages: ${report.fatal}`;
  return verbose ? formatVerbose(report) : formatSummary(report);
}

function main() {
  try {
    const options = parseArgs(process.argv.slice(2));
    const report = run(options);

    if (options.json) {
      console.log(JSON.stringify(report));
    } else {
      console.log(formatHuman(report, { verbose: options.verbose }));
    }

    if (report.fatal) process.exit(2);
    process.exit(report.missing.length === 0 ? 0 : 1);
  } catch (error) {
    console.error(`find-usages: ${error.message}`);
    process.exit(2);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}
