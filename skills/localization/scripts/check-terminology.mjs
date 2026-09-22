#!/usr/bin/env node
// Terminology consistency (criterion #3): find short English labels reused as
// the full value of several keys, then flag locales that translate those keys
// inconsistently. Output is REVIEW CANDIDATES, not verdicts: divergence can be
// legitimate context variance (e.g. "Close" the verb vs "Close" the adjective),
// so this never fails a build; it surfaces candidates for a human to judge.
//
//   node check-terminology.mjs [--path <repo>] [--catalog <id>] [--locale <name>] [--json]

import fs from "node:fs";
import { fileURLToPath } from "node:url";

import {
  parseReviewArgs as parseArgs,
  prepareCatalogReview,
  reviewCatalogLocales,
} from "./lib/review.mjs";

// A short label worth tracking as a term: trimmed, 1 to 3 words, <=30 chars,
// with no interpolation or markup (those are sentences/templates, not terms).
export function isTerm(value) {
  const t = value.trim();
  if (t.length === 0 || t.length > 30 || /%\(|[{}<>]/.test(t)) return false;
  const words = t.split(/\s+/);
  return words.length > 0 && words.length <= 3;
}

// term -> keys[], for terms used as the full value of >=2 distinct keys.
export function buildConcordance(enFlat) {
  const byTerm = new Map();
  for (const [key, value] of enFlat.entries()) {
    if (isTerm(value) === false) continue;
    const term = value.trim();
    if (!byTerm.has(term)) byTerm.set(term, []);
    byTerm.get(term).push(key);
  }
  const reused = new Map();
  for (const [term, keys] of byTerm) {
    if (keys.length >= 2) reused.set(term, keys);
  }
  return reused;
}

// For one locale, candidates where a reused term's keys are translated more than
// one way. Only translated keys count; untranslated keys are skipped.
// The renderings map for one reused term's keys: rendering text -> keys sharing
// it. Extracted to a helper so the `continue` (skipping untranslated keys) isn't
// nested inside `divergencesForLocale`'s own term loop.
function buildRenderingsByText(keys, localeFlat) {
  const byRendering = new Map();
  for (const key of keys) {
    const tr = localeFlat.get(key);
    if (tr === undefined) continue;
    if (!byRendering.has(tr)) byRendering.set(tr, []);
    byRendering.get(tr).push(key);
  }
  return byRendering;
}

export function divergencesForLocale(concordance, localeFlat, owners = null) {
  const candidates = [];
  for (const [term, keys] of concordance.entries()) {
    const byRendering = buildRenderingsByText(keys, localeFlat);
    if (byRendering.size <= 1) continue;
    const renderings = [...byRendering]
      .map(([text, ks]) => {
        const rendering = { text, count: ks.length, keys: ks };
        if (!owners) return rendering;

        const byFile = new Map();
        for (const key of ks) {
          const owner = owners.get(key);
          if (!owner) continue;
          if (!byFile.has(owner.file)) {
            byFile.set(owner.file, { ownedBy: owner.locale, file: owner.file, keys: [] });
          }
          byFile.get(owner.file).keys.push(key);
        }
        const ownership = byFile.values().toArray();
        if (ownership.length === 1) Object.assign(rendering, ownership[0]);
        else if (ownership.length > 1) rendering.owners = ownership;
        return rendering;
      })
      .sort((a, b) => b.count - a.count);
    const candidate = { term, keys, renderings };
    if (owners) {
      candidate.files = [
        ...new Set(
          renderings.flatMap((rendering) => {
            if (rendering.file) return [rendering.file];
            return (rendering.owners ?? []).map((owner) => owner.file);
          }),
        ),
      ];
      if (candidate.files.length === 1) {
        candidate.file = candidate.files[0];
        candidate.ownedBy = renderings.find((rendering) => rendering.ownedBy)?.ownedBy;
      }
    }
    candidates.push(candidate);
  }
  return candidates;
}

function run(options) {
  const review = prepareCatalogReview(options);
  if (review.fatal) return review;

  const concordance = buildConcordance(review.sourceFlat);
  const localeResults = reviewCatalogLocales(review, ({ effective }) =>
    divergencesForLocale(concordance, effective.values, effective.owners),
  );
  const { context } = review;

  const candidates = localeResults.reduce((sum, r) => sum + r.candidates.length, 0);
  return {
    repo: context.repo,
    catalog: context.id,
    syntax: context.syntax,
    reusedTerms: concordance.size,
    localeResults,
    totals: { candidates, locales: localeResults.length },
  };
}

const EXAMPLE_CAP = 25;
const KEYS_PER_RENDERING = 6;

// The keys under a rendering are the context that decides defect vs. legitimate
// variance (a button label vs. a menu vs. a header), so show them — capped, with
// the rest summarized, since the dominant rendering can cover many keys.
export function formatKeys(keys) {
  const shown = keys.slice(0, KEYS_PER_RENDERING).join(", ");
  const extra = keys.length - KEYS_PER_RENDERING;
  return extra > 0 ? `${shown} (+${extra} more)` : shown;
}

export function formatHuman(report) {
  if (report.fatal) return `check-terminology: ${report.fatal}`;

  const t = report.totals;
  const lines = [
    `check-terminology: ${report.repo} (${report.syntax}): ${report.reusedTerms} reused terms vs ${t.locales} locales`,
    "",
    `  ${t.candidates} terminology review candidate(s) (a reused label translated more than one way in a locale)`,
    `  Note: divergence can be legitimate context variance; these are candidates for review, not defects.`,
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
      lines.push(`  [${c.locale}] "${c.term}" (${c.keys.length} keys):`);
      for (const x of c.renderings) {
        const owner = x.file ? ` (${x.file})` : "";
        lines.push(`      ${JSON.stringify(x.text)} x${x.count}${owner} — ${formatKeys(x.keys)}`);
      }
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
