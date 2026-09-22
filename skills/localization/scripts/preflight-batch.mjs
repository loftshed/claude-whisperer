#!/usr/bin/env node
// Expand and validate one proposed translation batch before approval. This is
// the single read-only review gate: it verifies exact discovery-scope coverage,
// placeholders/markup/parseability, writer safety, and scoped advisory content
// checks, then emits the SHA-256 digest required by apply-translations.mjs.
//
//   node preflight-batch.mjs --scope <scope.json> [--path <repo>] [--catalog <id>] [--json] <batch.json|->
//
// Exit 0 when the batch is safe to approve, 1 when structural or coverage checks
// block it, and 2 for invalid input or repository configuration.

import fs from "node:fs";
import { fileURLToPath } from "node:url";

import { applyTranslations, keyOrderLines } from "./apply-translations.mjs";
import { candidatesForLocale, loadGeoTable } from "./check-content.mjs";
import { buildConcordance, divergencesForLocale } from "./check-terminology.mjs";
import {
  detectRepo,
  localeFiles,
  resolveCatalog,
  sourceLocale,
  translationsDir,
} from "./lib/adapters.mjs";
import {
  approvalDigest,
  BatchInputError,
  expandBatch,
  localeWriteSummary,
  readBatch,
  readScope,
  validateBatchSignatures,
  validateCoverage,
  validateScopeSources,
} from "./lib/batch.mjs";
import { effectiveCatalog, loadCatalogBundles } from "./lib/catalog.mjs";

const REVIEW_EXAMPLE_CAP = 8;

function takeValue(rest, flag) {
  const value = rest.shift();
  if (!value || value.startsWith("--")) {
    throw new BatchInputError(`${flag} requires a value`);
  }
  return value;
}

function applyArgument(argument, rest, options) {
  switch (argument) {
    case "--path": {
      options.path = takeValue(rest, argument);
      return;
    }
    case "--catalog": {
      options.catalog = takeValue(rest, argument);
      return;
    }
    case "--scope": {
      options.scopePath = takeValue(rest, argument);
      return;
    }
    case "--json": {
      options.json = true;
      return;
    }
    default: {
      if (argument.startsWith("--")) {
        throw new BatchInputError(`unknown argument ${argument}`);
      }
      if (options.payloadPath !== null) {
        throw new BatchInputError("provide at most one batch path");
      }
      options.payloadPath = argument;
    }
  }
}

export function parseArgs(argv) {
  const options = {
    path: null,
    catalog: null,
    scopePath: null,
    payloadPath: null,
    json: false,
  };
  const rest = [...argv];
  while (rest.length > 0) {
    const argument = rest.shift();
    applyArgument(argument, rest, options);
  }
  if (!options.scopePath) {
    throw new BatchInputError("--scope is required for exact approval coverage");
  }
  return options;
}

function groupByCatalog(entries) {
  const groups = new Map();
  for (const entry of entries) {
    if (!groups.has(entry.catalog)) groups.set(entry.catalog, []);
    groups.get(entry.catalog).push(entry);
  }
  return groups;
}

function virtualReview(entries, options) {
  const repo = detectRepo(options.path || process.cwd());
  if (repo.repo === "unknown") {
    throw new BatchInputError(`not a known i18n repo: ${repo.reason}`);
  }

  const terminology = [];
  const content = [];
  for (const [catalog, catalogEntries] of groupByCatalog(entries)) {
    const context = resolveCatalog(repo, catalog ?? options.catalog);
    if (context.error) throw new BatchInputError(context.error);
    if (context.geoKeyPrefixes) context.geoTable = loadGeoTable();
    const files = localeFiles(context);
    const { bundles, errors } = loadCatalogBundles(context, translationsDir(context), files);
    const sourceFailure = errors.get(sourceLocale(context));
    if (sourceFailure) {
      throw new BatchInputError(`cannot read source catalog: ${sourceFailure.error.message}`);
    }
    const source = bundles.get(sourceLocale(context)).parsed.values;
    const concordance = buildConcordance(source);
    const byLocale = new Map();
    for (const entry of catalogEntries) {
      if (!byLocale.has(entry.locale)) byLocale.set(entry.locale, []);
      byLocale.get(entry.locale).push(entry);
    }

    for (const [locale, localeEntries] of byLocale) {
      const parseFailure = errors.get(locale);
      if (parseFailure) {
        throw new BatchInputError(
          `cannot read ${parseFailure.file}: ${parseFailure.error.message}`,
        );
      }
      const effective = effectiveCatalog(context, locale, bundles);
      const values = new Map(effective.values);
      const pendingKeys = new Set();
      for (const entry of localeEntries) {
        values.set(entry.key, entry.value);
        pendingKeys.add(entry.key);
      }

      collectTerminologyCandidates(
        concordance,
        values,
        effective,
        pendingKeys,
        catalog,
        locale,
        terminology,
      );
      collectContentCandidates(
        source,
        values,
        locale,
        context,
        effective,
        pendingKeys,
        catalog,
        content,
      );
    }
  }
  return { terminology, content };
}

// Pulled out of the catalog/locale/candidate triple loop in virtualReview so
// each candidate loop's `continue` is local to its own function scope instead
// of reaching through the outer catalog/locale loops.
function collectTerminologyCandidates(
  concordance,
  values,
  effective,
  pendingKeys,
  catalog,
  locale,
  terminology,
) {
  for (const candidate of divergencesForLocale(concordance, values, effective.owners)) {
    if (candidate.keys.every((key) => !pendingKeys.has(key))) continue;
    terminology.push({ catalog, locale, ...candidate });
  }
}

function collectContentCandidates(
  source,
  values,
  locale,
  context,
  effective,
  pendingKeys,
  catalog,
  content,
) {
  const candidates = candidatesForLocale(source, values, locale, {
    ...context,
    owners: effective.owners,
  });
  for (const candidate of candidates) {
    if (!pendingKeys.has(candidate.key)) continue;
    content.push({ catalog, locale, ...candidate });
  }
}

function pairSummary(pair) {
  const [catalog, locale, key] = pair.split("\0", 3);
  return `${catalog ? `${catalog}:` : ""}${locale}:${key}`;
}

export function run(options, payload, scope) {
  const expanded = expandBatch(payload, options);
  const entries = expanded.entries;
  const coverage = validateCoverage(entries, scope);
  const sourceIssues = validateScopeSources(scope, options);
  const structureIssues = validateBatchSignatures(entries, options);

  let writer = null;
  let writerError = null;
  try {
    writer = applyTranslations(
      {
        path: options.path,
        catalog: options.catalog,
        dryRun: true,
        force: false,
        approvedDigest: null,
      },
      entries,
    );
  } catch (error) {
    if (error.exitCode === 2) throw new BatchInputError(error.message);
    writerError = error.message;
  }

  const advisory =
    !writerError && coverage.ok && sourceIssues.length === 0 && structureIssues.length === 0
      ? virtualReview(entries, options)
      : { terminology: [], content: [] };
  const keyIdentities = new Set(entries.map((entry) => `${entry.catalog}\0${entry.key}`));
  const locales = new Set(entries.map((entry) => entry.locale));
  const catalogs = new Set(entries.map((entry) => entry.catalog));
  const ready =
    coverage.ok && sourceIssues.length === 0 && structureIssues.length === 0 && !writerError;
  const digest = ready ? approvalDigest(entries, scope, writer.targets) : null;

  return {
    ready,
    digest,
    totals: {
      translations: entries.length,
      keys: keyIdentities.size,
      locales: locales.size,
      catalogs: catalogs.size,
      ...expanded.provenance,
    },
    coverage: {
      ok: coverage.ok,
      missing: coverage.missing.map(pairSummary),
      extra: coverage.extra.map(pairSummary),
      operationMismatches: coverage.operationMismatches.map((mismatch) => ({
        ...mismatch,
        pair: pairSummary(mismatch.pair),
      })),
    },
    source: { ok: sourceIssues.length === 0, issues: sourceIssues },
    structure: { ok: structureIssues.length === 0, issues: structureIssues },
    writer: writer
      ? {
          ok: true,
          repo: writer.repo,
          writes: localeWriteSummary(entries),
          localeResults: writer.localeResults,
        }
      : { ok: false, error: writerError },
    review: {
      manifest: expanded.review,
      terminology: advisory.terminology,
      content: advisory.content,
    },
  };
}

function reviewLabel(candidate) {
  if (typeof candidate === "string") return candidate;
  if (!candidate || typeof candidate !== "object") return JSON.stringify(candidate);
  if (candidate.term) {
    return `${candidate.locale} term ${JSON.stringify(candidate.term)} has ${candidate.renderings.length} renderings`;
  }
  if (candidate.code) return `${candidate.locale} ${candidate.key}: ${candidate.code}`;
  if (candidate.tag) {
    const key = candidate.key ? `${candidate.key} ` : "";
    const reason = candidate.reason ? ` — ${candidate.reason}` : "";
    return `${key}${candidate.tag}${reason}`;
  }
  return candidate.message ?? candidate.key ?? JSON.stringify(candidate);
}

// One round-robin pass over the queues, taking one item from each queue that
// still has one, until `limit` is reached. Pulled out of the while loop below
// so the per-queue `continue`/`break` are local to this function's own loop.
function fillOneRound(queues, limit, selected) {
  for (const queue of queues) {
    if (selected.length >= limit) return;
    if (queue.index >= queue.values.length) continue;
    selected.push(queue.values[queue.index]);
    queue.index += 1;
  }
}

// Keep the bounded human summary representative when one category is much
// larger than the others. A manifest-heavy review must not force the agent to
// dump JSON merely to discover that terminology or content candidates exist.
export function sampleReviewDetails(reviewGroups, limit) {
  const queues = reviewGroups
    .filter(([, candidates]) => candidates.length > 0)
    .map(([name, candidates]) => ({
      name,
      values: candidates.map((candidate) => `[${name}] ${reviewLabel(candidate)}`),
      index: 0,
    }));
  const selected = [];
  while (selected.length < limit && queues.some((queue) => queue.index < queue.values.length)) {
    fillOneRound(queues, limit, selected);
  }
  return selected;
}

export function formatHuman(report) {
  const state = report.ready ? "ready" : "blocked";
  const totals = report.totals;
  const lines = [
    `preflight-batch: ${state}${report.digest ? ` ${report.digest}` : ""}`,
    `  ${totals.translations} translation(s) / ${totals.keys} key(s) / ${totals.locales} locale(s); ${totals.copied} copied, ${totals.authored} authored`,
    report.coverage.ok
      ? "  coverage: exact discovery scope"
      : `  coverage: ${report.coverage.missing.length} missing, ${report.coverage.extra.length} extra, ${(report.coverage.operationMismatches ?? []).length} operation mismatch(es)`,
    report.source?.ok
      ? "  source: unchanged since discovery"
      : `  source: ${report.source?.issues.length ?? 0} blocking change(s)`,
    report.structure.ok
      ? "  structure: placeholders, markup, and syntax clean"
      : `  structure: ${report.structure.issues.length} blocking issue(s)`,
  ];

  if (report.writer.ok) {
    const writes = report.writer.writes;
    const count =
      writes.uniformCount === null
        ? `${writes.locales} locale file(s), non-uniform counts`
        : `${writes.locales} locale file(s) × ${writes.uniformCount}`;
    lines.push(`  writer: dry-run clean (${count})`);
    const localeResults = report.writer.localeResults ?? [];
    const catalogs = new Set(localeResults.map((result) => result.catalog));
    lines.push(...keyOrderLines(localeResults, catalogs.size > 1));
  } else {
    lines.push(`  writer: blocked — ${report.writer.error}`);
  }

  const reviewGroups = [
    ["manifest", report.review.manifest ?? []],
    ["terminology", report.review.terminology ?? []],
    ["content", report.review.content ?? []],
  ];
  const reviewTotal = reviewGroups.reduce((sum, [, candidates]) => sum + candidates.length, 0);
  lines.push(
    `  review: ${reviewTotal} candidate(s) (${reviewGroups.map(([name, values]) => `${values.length} ${name}`).join(", ")})`,
  );

  const blocking = [
    ...report.coverage.missing.map((item) => `missing ${item}`),
    ...report.coverage.extra.map((item) => `extra ${item}`),
    ...(report.coverage.operationMismatches ?? []).map(
      (item) => `operation ${item.pair}: expected ${item.expected}, got ${item.actual}`,
    ),
    ...(report.source?.issues ?? []).map((issue) => `${issue.catalog}:${issue.key} ${issue.code}`),
    ...report.structure.issues.map((issue) => `${issue.locale}:${issue.key} ${issue.code}`),
  ];
  const visibleBlocking = blocking.slice(0, REVIEW_EXAMPLE_CAP);
  const visibleReview = sampleReviewDetails(
    reviewGroups,
    REVIEW_EXAMPLE_CAP - visibleBlocking.length,
  );
  const visibleDetails = [...visibleBlocking, ...visibleReview];
  for (const detail of visibleDetails) lines.push(`    ${detail}`);
  const detailTotal = blocking.length + reviewTotal;
  if (detailTotal > visibleDetails.length) {
    lines.push(`    +${detailTotal - visibleDetails.length} more (use --json for the full report)`);
  }
  return lines.join("\n");
}

function main() {
  try {
    const options = parseArgs(process.argv.slice(2));
    const payload = readBatch(options.payloadPath);
    const scope = readScope(options.scopePath);
    const report = run(options, payload, scope);
    console.log(options.json ? JSON.stringify(report) : formatHuman(report));
    if (!report.ready) process.exitCode = 1;
  } catch (error) {
    console.error(`preflight-batch: ${error.message}`);
    process.exit(error.exitCode ?? 1);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}
