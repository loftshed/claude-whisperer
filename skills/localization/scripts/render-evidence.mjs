#!/usr/bin/env node
// Render selected translation-memory anchors from a staged evidence report.
// The report keeps the complete machine-readable locale vectors; this command
// exposes only the fuzzy or exact anchors currently needed for authoring,
// without JSON object scaffolding or repeated key prefixes.
//
//   node render-evidence.mjs <evidence.json|-> --key <pending-key> [--key <pending-key> ...] [--locale <name> ...]

import fs from "node:fs";
import { fileURLToPath } from "node:url";

import { sharedNamespace } from "./lib/keys.mjs";

export class EvidenceInputError extends Error {
  constructor(message) {
    super(message);
    this.name = "EvidenceInputError";
    this.exitCode = 2;
  }
}

export const USAGE = `Usage: node render-evidence.mjs <evidence.json|-> --key <pending-key> [options]

Render a compact locale matrix for selected anchors in staged evidence.

Options:
  --key <pending-key>      Pending key to inspect (repeatable, required)
  --locale <name>          Locale to include (repeatable; defaults to every locale)
  --help                   Show this help`;

function takeValue(rest, flag) {
  const value = rest.shift();
  if (!value || value.startsWith("--")) {
    throw new EvidenceInputError(`${flag} requires a value`);
  }
  return value;
}

function applyArgument(argument, rest, options) {
  switch (argument) {
    case "--key": {
      options.keys.push(takeValue(rest, argument));
      return;
    }
    case "--locale": {
      options.locales.push(takeValue(rest, argument));
      return;
    }
    case "--help": {
      options.help = true;
      return;
    }
    default: {
      if (argument.startsWith("--")) {
        throw new EvidenceInputError(`unknown argument ${argument}`);
      }
      if (options.evidencePath !== null) {
        throw new EvidenceInputError("provide exactly one evidence path");
      }
      options.evidencePath = argument;
    }
  }
}

export function parseArgs(argv) {
  const options = {
    evidencePath: null,
    keys: [],
    locales: [],
    help: false,
  };
  const rest = [...argv];
  while (rest.length > 0) {
    const argument = rest.shift();
    applyArgument(argument, rest, options);
  }
  options.keys = [...new Set(options.keys)];
  options.locales = [...new Set(options.locales)];
  if (!options.help) {
    if (!options.evidencePath) {
      throw new EvidenceInputError("provide an evidence JSON path or - for stdin");
    }
    if (options.keys.length === 0) {
      throw new EvidenceInputError("select at least one pending key with --key");
    }
  }
  return options;
}

export function readEvidence(evidencePath) {
  let raw;
  try {
    raw = evidencePath === "-" ? fs.readFileSync(0, "utf8") : fs.readFileSync(evidencePath, "utf8");
  } catch (error) {
    throw new EvidenceInputError(`cannot read evidence: ${error.message}`);
  }
  try {
    const report = JSON.parse(raw);
    if (!Array.isArray(report?.results)) {
      throw new EvidenceInputError("evidence must contain a results array");
    }
    return report;
  } catch (error) {
    if (error instanceof EvidenceInputError) throw error;
    throw new EvidenceInputError(`cannot parse evidence JSON: ${error.message}`);
  }
}

function compactKey(key, namespace) {
  return namespace && key.startsWith(`${namespace}.`) ? key.slice(namespace.length + 1) : key;
}

// One row's translated-locale names folded into `seenLocales`/`availableLocales`,
// pulled out of the rows loop below so the per-locale `continue` is local to
// this function's own loop instead of nesting inside the rows loop.
function collectLocalesFromMatch(match, seenLocales, availableLocales) {
  const localeNames = Object.keys(match?.translations ?? {});
  for (const locale of localeNames) {
    if (seenLocales.has(locale)) continue;
    seenLocales.add(locale);
    availableLocales.push(locale);
  }
}

export function selectEvidence(report, options) {
  const byPendingKey = new Map();
  for (const [index, result] of report.results.entries()) {
    if (!result || !Array.isArray(result.pendingKeys) || !Array.isArray(result.matches)) {
      throw new EvidenceInputError(
        `evidence results[${index}] must contain pendingKeys and matches arrays`,
      );
    }
    for (const key of result.pendingKeys) {
      if (byPendingKey.has(key)) {
        throw new EvidenceInputError(`pending key ${key} appears in more than one result`);
      }
      byPendingKey.set(key, result);
    }
  }

  const rows = options.keys.map((key) => {
    const result = byPendingKey.get(key);
    if (!result) throw new EvidenceInputError(`pending key ${key} is not in the evidence report`);
    return { key, result, match: result.matches[0] ?? null };
  });

  const availableLocales = [];
  const seenLocales = new Set();
  for (const { match } of rows) {
    collectLocalesFromMatch(match, seenLocales, availableLocales);
  }
  const locales = options.locales.length > 0 ? options.locales : availableLocales;
  for (const locale of locales) {
    if (!seenLocales.has(locale)) {
      throw new EvidenceInputError(`locale ${locale} is not present in the selected anchors`);
    }
  }
  return { rows, locales };
}

export function formatHuman(selection) {
  const { rows, locales } = selection;
  const namespace = sharedNamespace(rows.map((row) => row.key));
  const anchorCount = rows.filter((row) => row.match).length;
  const lines = [
    `evidence-detail: ${rows.length} pending key(s), ${anchorCount} anchor(s), ${locales.length} locale(s)`,
    ...(namespace ? [`  namespace: ${namespace}.*`] : []),
  ];

  for (const [index, row] of rows.entries()) {
    const label = compactKey(row.key, namespace);
    if (!row.match) {
      lines.push(`  [${index + 1}] ${label} — fresh; no existing anchor`);
      continue;
    }
    const score = row.match.score === 1 ? "exact" : Number(row.match.score).toFixed(2);
    lines.push(
      `  [${index + 1}] ${label} ← ${row.match.key} [${score}] — EN ${JSON.stringify(row.match.source)}`,
    );
  }

  if (anchorCount > 0 && locales.length > 0) {
    lines.push(`  locale values in [1]..[${rows.length}] order:`);
  }
  for (const locale of locales) {
    const values = rows.map((row) => {
      if (!row.match) return "—";
      const value = row.match.translations?.[locale];
      return value == null ? "(untranslated)" : JSON.stringify(value);
    });
    lines.push(`    ${locale}: ${values.join(" | ")}`);
  }
  return lines.join("\n");
}

function main() {
  try {
    const options = parseArgs(process.argv.slice(2));
    if (options.help) {
      console.log(USAGE);
      return;
    }
    const report = readEvidence(options.evidencePath);
    console.log(formatHuman(selectEvidence(report, options)));
  } catch (error) {
    console.error(`render-evidence: ${error.message}`);
    process.exit(error.exitCode ?? 1);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}
