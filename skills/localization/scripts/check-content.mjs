#!/usr/bin/env node
// Advisory content heuristics (Step 2 helper): per-string checks that a script
// can do deterministically but that are NOT hard breakage, so they never fail
// the build. Three checks, all REVIEW CANDIDATES for a human to judge:
//
//   identical-source  locale value copied from English, not translated
//   length-overflow   translation far longer than source; may not fit the UI
//   brand-drift       a keep-in-English term altered or dropped in a locale
//
//   node check-content.mjs [--path <repo>] [--catalog <id>] [--locale <name>] [--json]
//
// Exit is always 0 (or 2 on a fatal config error): this surfaces candidates,
// it does not gate. The structural gate (validate-structure.mjs) is the gate.

import fs from "node:fs";
import { fileURLToPath } from "node:url";

import {
  brandDriftCandidate,
  geoMismatchCandidate,
  identicalCandidate,
  lengthCandidate,
} from "./lib/content-rules.mjs";
import {
  parseReviewArgs as parseArgs,
  prepareCatalogReview,
  reviewCatalogLocales,
} from "./lib/review.mjs";

// Bundled per-locale reference names for geographic proper nouns (see
// lib/geo-names.json). Loaded once; a missing or unreadable table leaves the
// geo audit inert rather than failing this advisory check.
export function loadGeoTable() {
  try {
    const file = fileURLToPath(new URL("lib/geo-names.json", import.meta.url));
    return JSON.parse(fs.readFileSync(file, "utf8")).names ?? {};
  } catch {
    return {};
  }
}

// Ranked for review: the most objective signal (a copied string) first, then
// the worst-fitting length overflows, then brand drift.
const CODE_RANK = {
  "identical-source": 0,
  "geo-mismatch": 1,
  "length-overflow": 2,
  "brand-drift": 3,
};

// The non-null candidates found for one key, each annotated with its owning
// locale/file when the key is owned elsewhere (see `context.owners`).
function ownedCandidates(found, key, context) {
  const owned = [];
  for (const candidate of found) {
    if (!candidate) continue;
    const owner = context.owners?.get(key);
    owned.push(owner ? { ...candidate, ownedBy: owner.locale, file: owner.file } : candidate);
  }
  return owned;
}

// Apply the three checks to every translated key in one locale. Untranslated
// keys are skipped here; the structural gate already reports them.
export function candidatesForLocale(sourceFlat, localeFlat, locale, context) {
  const options = {
    ...(context.geoKeyPrefixes && { geoPrefixes: context.geoKeyPrefixes }),
    ...(context.geoTable && { geoTable: context.geoTable }),
    ...(context.keepInEnglish && { keepInEnglish: context.keepInEnglish }),
  };
  const out = [];
  for (const [key, source] of sourceFlat.entries()) {
    const translation = localeFlat.get(key);
    if (translation === undefined) continue;
    const found = [
      identicalCandidate(key, source, translation, locale, options),
      geoMismatchCandidate(key, source, translation, locale, options),
      lengthCandidate(key, source, translation),
      brandDriftCandidate(key, source, translation, context.brands ?? []),
    ];
    out.push(...ownedCandidates(found, key, context));
  }
  // Within geo-mismatch, untranslated (high-precision missed translations)
  // outranks diverges (style divergences with legitimate exceptions).
  const reasonRank = (c) => (c.reason === "untranslated" ? 0 : 1);
  out.sort(
    (a, b) =>
      CODE_RANK[a.code] - CODE_RANK[b.code] ||
      reasonRank(a) - reasonRank(b) ||
      (b.ratio ?? 0) - (a.ratio ?? 0) ||
      a.key.localeCompare(b.key),
  );
  return out;
}

function run(options) {
  const review = prepareCatalogReview(options);
  if (review.fatal) return review;

  const { context } = review;
  if (context.geoKeyPrefixes) context.geoTable = loadGeoTable();
  const localeResults = reviewCatalogLocales(review, ({ effective, locale, sourceFlat }) =>
    candidatesForLocale(sourceFlat, effective.values, locale, {
      ...context,
      owners: effective.owners,
    }),
  );

  const byCode = {
    "identical-source": 0,
    "geo-mismatch": 0,
    "length-overflow": 0,
    "brand-drift": 0,
  };
  for (const r of localeResults) {
    for (const c of r.candidates) byCode[c.code] += 1;
  }
  const candidates = localeResults.reduce((sum, r) => sum + r.candidates.length, 0);
  return {
    repo: context.repo,
    catalog: context.id,
    syntax: context.syntax,
    localeResults,
    totals: { candidates, byCode, locales: localeResults.length },
  };
}

const EXAMPLE_CAP = 30;

function describe(c) {
  switch (c.code) {
    case "identical-source": {
      return `${c.key} — copied from English, not translated: ${JSON.stringify(c.translation)}`;
    }
    case "geo-mismatch": {
      const detail =
        c.reason === "untranslated"
          ? `left in English; reference name is ${JSON.stringify(c.expected)}`
          : `${JSON.stringify(c.translation)} differs from the reference name ${JSON.stringify(c.expected)}`;
      return `${c.key} — ${detail}`;
    }
    case "length-overflow": {
      const t = c.type === "general" ? "" : ` [${c.type}]`;
      return `${c.key} — ${c.severity} ${c.ratio}x the English length (${c.trChars} vs ${c.srcChars} chars)${t}`;
    }
    case "brand-drift": {
      return `${c.key} — keep-in-English term(s) altered or dropped: ${c.terms.join(", ")}`;
    }
    default: {
      return c.key;
    }
  }
}

export function formatHuman(report) {
  if (report.fatal) return `check-content: ${report.fatal}`;

  const t = report.totals;
  const lines = [
    `check-content: ${report.repo} (${report.syntax}): ${t.candidates} review candidate(s) across ${t.locales} locales`,
    "",
    `  · ${t.byCode["identical-source"]} identical-source (copied, not translated)`,
    `  · ${t.byCode["geo-mismatch"]} geo-mismatch (differs from the reference place name)`,
    `  · ${t.byCode["length-overflow"]} length-overflow (may not fit the UI)`,
    `  · ${t.byCode["brand-drift"]} brand-drift (keep-in-English term changed)`,
    `  Note: heuristics with real false positives; these are candidates for review, not defects.`,
  ];

  const flat = [];
  for (const r of report.localeResults) {
    if (r.parseError) flat.push({ locale: r.locale, file: r.file, parseError: r.parseError });
    for (const c of r.candidates) flat.push({ locale: r.locale, ...c });
  }

  if (flat.length > 0) {
    lines.push("");
    for (const c of flat.slice(0, EXAMPLE_CAP)) {
      if (c.parseError) {
        lines.push(`  [parse-error] ${c.locale} (${c.file}): ${c.parseError}`);
        continue;
      }
      const owner = c.file ? `; ${c.file}` : "";
      lines.push(`  [${c.code}] (${c.locale}${owner}) ${describe(c)}`);
    }
    if (flat.length > EXAMPLE_CAP) {
      lines.push(`  (+${flat.length - EXAMPLE_CAP} more; run with --json for the full list)`);
    }
  }

  return lines.join("\n");
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  const report = run(options);

  if (options.json) {
    console.log(JSON.stringify(report));
  } else {
    console.log(formatHuman(report));
  }

  if (report.fatal) process.exit(2);
  process.exit(0);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}

export { parseReviewArgs as parseArgs } from "./lib/review.mjs";
