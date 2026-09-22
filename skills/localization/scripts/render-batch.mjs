#!/usr/bin/env node
// Summarize a proposed translation batch for the one approval gate. Reads the
// same JSON array apply-translations.mjs consumes
// ({ catalog, locale, key, value }) on stdin, attaches each key's English
// source, and prints one line per source key. --full expands every proposed
// locale value when a user explicitly asks to inspect the complete matrix.
// Read-only; repo-aware.
//
//   node render-batch.mjs - [--path <repo>] [--catalog <id>] [--full] [--json]
//
// Exit 0 on success, 2 on a fatal config or input error.
//
// --json output shape: { groups: [{ key, source, rows: [{ locale, value }] }],
// missingSource: [key], totals: { keys, sourceStrings, locales,
// translations } } — sourceStrings counts distinct English values (null when
// the source catalog is unavailable), and missingSource lists batch keys
// absent from the English source, which apply-translations.mjs will refuse.

import fs from "node:fs";
import { fileURLToPath } from "node:url";

import {
  detectRepo,
  localeFiles,
  resolveCatalog,
  sourceFile,
  sourceLocale,
  translationsDir,
} from "./lib/adapters.mjs";
import { loadCatalogBundles } from "./lib/catalog.mjs";

export class LocalizationInputError extends Error {
  constructor(message) {
    super(message);
    this.name = "LocalizationInputError";
    this.exitCode = 2;
  }
}

function applyArgument(a, rest, options) {
  switch (a) {
    case "--path": {
      options.path = rest.shift();
      return;
    }
    case "--catalog": {
      options.catalog = rest.shift();
      return;
    }
    case "--json": {
      options.json = true;
      return;
    }
    case "--full": {
      options.full = true;
      return;
    }
    // No default: bare "-" (stdin marker) and unknown flags are ignored.
  }
}

export function parseArgs(argv) {
  const options = { path: null, catalog: null, full: false, json: false };
  const rest = [...argv];
  while (rest.length > 0) {
    const a = rest.shift();
    applyArgument(a, rest, options);
  }
  return options;
}

// Collapse flat {locale, key, value} rows into per-key groups, preserving the
// order keys first appear and sorting each key's locales.
export function groupByKey(entries) {
  const order = [];
  const byKey = new Map();
  for (const { key, locale, value } of entries) {
    if (!byKey.has(key)) {
      byKey.set(key, []);
      order.push(key);
    }
    byKey.get(key).push({ locale, value });
  }
  return order.map((key) => ({
    key,
    rows: byKey.get(key).sort((a, b) => a.locale.localeCompare(b.locale)),
  }));
}

// English source per key, loaded once. Source is advisory context for the grid,
// so a missing repo or unreadable catalog leaves every source null rather than
// failing — apply-translations.mjs is where key/source validity is enforced.
function loadSource(options) {
  const repo = detectRepo(options.path || process.cwd());
  if (repo.repo === "unknown") return new Map();
  const context = resolveCatalog(repo, options.catalog);
  if (context.error) return new Map();
  try {
    const files = localeFiles(context);
    if (!files.includes(sourceFile(context))) return new Map();
    const { bundles, errors } = loadCatalogBundles(context, translationsDir(context), files);
    if (errors.get(sourceLocale(context))) return new Map();
    return bundles.get(sourceLocale(context)).parsed.values;
  } catch {
    return new Map();
  }
}

export function run(entries, options = {}) {
  if (!Array.isArray(entries) || entries.length === 0) {
    throw new LocalizationInputError("empty batch: expected a non-empty JSON array on stdin");
  }
  const source = loadSource(options);
  const groups = groupByKey(entries).map((group) => ({
    key: group.key,
    source: source.get(group.key) ?? null,
    rows: group.rows,
  }));
  const locales = new Set(entries.map((entry) => entry.locale));
  // With no readable source catalog every source is null; only a per-key miss
  // against a loaded catalog is an orphan the writer will refuse.
  const missingSource =
    source.size === 0 ? [] : groups.filter((group) => group.source === null).map((g) => g.key);
  const sourceStrings =
    source.size === 0
      ? null
      : new Set(groups.map((group) => group.source).filter((value) => value !== null)).size;
  return {
    groups,
    missingSource,
    totals: {
      keys: groups.length,
      sourceStrings,
      locales: locales.size,
      translations: entries.length,
    },
  };
}

export function formatHuman(report, { full = false } = {}) {
  const { keys, sourceStrings, locales, translations } = report.totals;
  const distinct = sourceStrings === null ? "" : ` (${sourceStrings} distinct English string(s))`;
  const lines = [
    `batch: ${translations} proposed translation(s) for ${keys} source key(s)${distinct} across ${locales} locale(s)`,
    "",
    ...(report.missingSource.length > 0
      ? [
          `  ⚠ ${report.missingSource.length} key(s) missing from the English source — apply-translations will refuse them: ${report.missingSource.join(", ")}`,
          "",
        ]
      : []),
  ];
  for (const group of report.groups) {
    const source = group.source === null ? "(source unavailable)" : JSON.stringify(group.source);
    if (!full) {
      lines.push(`  ${group.key} — ${source} (${group.rows.length} locale(s))`);
      continue;
    }
    lines.push(`## ${group.key}`, `  EN: ${source}`);
    for (const { locale, value } of group.rows) lines.push(`  ${locale}: ${JSON.stringify(value)}`);
    lines.push("");
  }
  if (!full) lines.push("", "Use --full only when the complete locale-value matrix is requested.");
  return lines.join("\n").trimEnd();
}

function readStdin() {
  try {
    return fs.readFileSync(0, "utf8");
  } catch (error) {
    throw new LocalizationInputError(`cannot read stdin: ${error.message}`);
  }
}

function main() {
  try {
    const options = parseArgs(process.argv.slice(2));
    let entries;
    try {
      entries = JSON.parse(readStdin());
    } catch (error) {
      throw new LocalizationInputError(`invalid JSON on stdin: ${error.message}`);
    }
    const report = run(entries, options);
    console.log(
      options.json ? JSON.stringify(report) : formatHuman(report, { full: options.full }),
    );
  } catch (error) {
    console.error(`render-batch: ${error.message}`);
    process.exit(error.exitCode ?? 1);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}
