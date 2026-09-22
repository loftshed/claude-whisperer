#!/usr/bin/env node
// Find where a translation key lives in a catalog file, by its dotted object
// path (JSON/TypeScript) or literal flat key (properties), and print a clickable
// `file:line`. Catalog-aware via the same adapter as the rest of the skill:
// nested key styles traverse the path; flat key styles and properties bundles
// match keys literally. For a properties entry
// spanning continuation lines, the reported line is the logical line's start.
// Read-only — it never edits, it reports a location.
//
//   node locate-key.mjs <key> [<key> ...] [--path <repo>] [--catalog <id>] [--locale <name>] [--all] [--json]
//
//   default        look up the key in the source file
//   --locale fr    look it up in one target locale (line numbers differ per file)
//   --all          report the line and value in every locale file
//   --json         machine-readable result (one report per key when several are given)
//
// Several keys in one call is the authoring case: `--all` with an anchor set
// dumps each key's shipped wording across every locale, which is the input fresh
// translation needs and the reason no catalog should be probed by hand.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  detectRepo,
  localeFiles,
  localeForFile,
  resolveCatalog,
  sourceFile,
  translationsDir,
} from "./lib/adapters.mjs";
import { locate } from "./lib/locate.mjs";
import { parseProperties } from "./lib/properties.mjs";
import { parseTypeScriptCatalog, typeScriptEntries } from "./lib/typescript-catalog.mjs";

// Applies one shifted-off argument to `options` (mutated in place), consuming
// its value from `rest` when it takes one. Kept as its own function so the
// per-argument `switch` isn't nested inside the `parseArgs` loop.
function applyArg(a, rest, options) {
  switch (a) {
    case "--json": {
      options.json = true;
      break;
    }
    case "--all": {
      options.all = true;
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
    case "--locale": {
      options.locale = rest.shift();
      break;
    }
    case "--key": {
      const value = rest.shift();
      if (value) options.keys.push(value);
      break;
    }
    default: {
      if (!a.startsWith("--")) options.keys.push(a);
    }
  }
}

export function parseArgs(argv) {
  const options = {
    json: false,
    path: null,
    catalog: null,
    locale: null,
    all: false,
    keys: [],
  };
  const rest = [...argv];
  while (rest.length > 0) {
    applyArg(rest.shift(), rest, options);
  }
  // `key` stays the single-lookup field `run` reads; `keys` is what the CLI
  // iterates, so one call covers a whole anchor set.
  options.keys = [...new Set(options.keys)];
  options.key = options.keys[0] ?? null;
  return options;
}

// Locate-shaped entries for a properties buffer: one per key, positioned at the
// logical line's physical start.
function propertiesEntries(buffer) {
  const parsed = parseProperties(buffer);
  return parsed.values
    .entries()
    .map(([key, value]) => ({
      path: key,
      line: parsed.locations.get(key).startLine,
      endLine: parsed.locations.get(key).endLine,
      kind: "string",
      value,
    }))
    .toArray();
}

export function run(options) {
  if (!options.key) return { fatal: "no key given (e.g. app.generic.close)" };

  const repo = detectRepo(options.path || process.cwd());
  if (repo.repo === "unknown") return { fatal: `not a known i18n repo: ${repo.reason}` };
  const context = resolveCatalog(repo, options.catalog);
  if (context.error) return { fatal: context.error };

  const dir = translationsDir(context);
  let files;
  try {
    // deepcode ignore: directory comes from the detected repository, not untrusted input; local run with the user's own permissions.
    files = localeFiles(context);
  } catch (error) {
    return { fatal: `cannot read ${dir}: ${error.message}` };
  }

  let selected;
  if (options.all) {
    selected = files;
  } else if (options.locale) {
    const want = files.find(
      (file) => file === options.locale || localeForFile(context, file) === options.locale,
    );
    if (!want) return { fatal: `locale ${options.locale} not found` };
    selected = [want];
  } else {
    selected = [sourceFile(context)];
  }

  // One scan per file; matches and child-key suggestions both derive from it.
  const childPrefix = `${options.key}.`;
  const results = selected.map((file) => {
    const abs = path.join(dir, file);
    const rel = path.join(context.dir, file);
    let entries;
    try {
      if (context.format === "properties") {
        entries = propertiesEntries(fs.readFileSync(abs));
      } else if (context.format === "typescript") {
        entries = typeScriptEntries(parseTypeScriptCatalog(fs.readFileSync(abs), context));
      } else {
        entries = locate(fs.readFileSync(abs, "utf8"));
      }
    } catch (error) {
      return { file, rel, readError: error.message, matches: [] };
    }
    return {
      file,
      rel,
      matches: entries.filter((e) => e.path === options.key),
      suggestions: entries.filter((e) => e.path.startsWith(childPrefix)).map((e) => e.path),
    };
  });

  const found = results.reduce((sum, r) => sum + r.matches.length, 0);
  return { repo: context.repo, catalog: context.id, key: options.key, results, found };
}

function formatHuman(report, { withHeader = false } = {}) {
  if (report.fatal) return `locate-key: ${report.fatal}`;

  const lines = withHeader ? [`${report.key}:`] : [];
  for (const r of report.results) {
    if (r.readError) {
      lines.push(`  [read-error] ${r.rel}: ${r.readError}`);
      continue;
    }
    for (const m of r.matches) {
      const detail =
        m.kind === "string" || m.kind === "literal"
          ? `  →  ${JSON.stringify(m.value)}`
          : `  →  (${m.kind})`;
      lines.push(`${r.rel}:${m.line}${detail}`);
    }
  }

  if (report.found === 0) {
    lines.push(`locate-key: "${report.key}" not found in ${report.repo}`);
    const suggestions = [...new Set(report.results.flatMap((r) => r.suggestions ?? []))];
    if (suggestions.length > 0) {
      lines.push("  did you mean one of its child keys?");
      for (const s of suggestions.slice(0, 8)) lines.push(`    ${s}`);
      if (suggestions.length > 8) lines.push(`    (+${suggestions.length - 8} more)`);
    }
  }

  return lines.join("\n");
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  const keys = options.keys.length > 0 ? options.keys : [null];
  const reports = keys.map((key) => run({ ...options, key }));
  const isMultiple = reports.length > 1;

  if (options.json) {
    console.log(JSON.stringify(isMultiple ? reports : reports[0]));
  } else {
    console.log(
      reports.map((report) => formatHuman(report, { withHeader: isMultiple })).join("\n"),
    );
  }

  if (reports.some((report) => report.fatal || report.results.some((result) => result.readError))) {
    process.exit(2);
  }
  process.exit(reports.every((report) => report.found > 0) ? 0 : 1);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}
