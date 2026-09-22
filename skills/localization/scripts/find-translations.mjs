#!/usr/bin/env node
// Translation-memory lookup: given one or more English strings, find where the
// same or a similar English value already lives in the source catalog and print
// what each shipped locale rendered it as. This grounds a new translation in an
// existing in-repo rendering instead of guessing — the first authority in
// references/authority-stack.md. Read-only; repo-aware via the same adapter as
// the rest of the skill.
//
//   node find-translations.mjs "<english>" ["<english>" ...] \
//     [--fuzzy | --fallback-fuzzy] [--threshold <0..1>] [--limit <n>] \
//     [--path <repo>] [--catalog <id>] [--locale <name>] [--json]
//   node find-translations.mjs --report <untranslated-report.json|-> \
//     [--fallback-fuzzy] [--limit <n>] [--siblings] [--prefix <ns> ...] \
//     [--json | --out <file>]
//
//   --fuzzy          also report near-matches, not just exact ones
//   --fallback-fuzzy report near-matches only when no exact match exists
//   --report path     take every unique source string from an untranslated
//                     JSON report (flat `entries` or --group-by-key `groups`)
//                     and exclude the pending keys themselves
//   --siblings       with --report, also emit the already-translated keys that
//                     share a pending key's namespace prefix, with their
//                     per-locale renderings
//   --sibling-limit n cap sibling keys per namespace (default 25)
//   --all-siblings   return every sibling under each requested namespace
//   --prefix ns      also emit the translated keys under an explicit namespace
//                     (repeatable, works with or without --report) — the family
//                     around a match that lives in another component
//   --out file       write the full JSON evidence to <file> and print only a
//                     compact triage view (match keys and scores, sibling key
//                     names and sources — no per-locale payload) to stdout
//   --threshold n    minimum similarity for a fuzzy match (default 0.6)
//   --limit n        max matches reported per query (default 10)
//   --locale name    report only this locale's rendering
//
// Matching is placeholder- and case-insensitive: "Welcome %(name)s" matches
// "Welcome {name}". Exit 0 always, 2 on a fatal config error.
//
// --json output shape: { repo, catalog, results: [{ query, pendingKeys,
// exactConsensus?, matches: [{ key, equivalentKeys?, source, score,
// translations: { <locale>: value|null } }] }] }, plus with --siblings:
// siblings: [{ prefix, keys: [{ key, source, translations }], omitted? }].

import fs from "node:fs";
import { fileURLToPath } from "node:url";

import {
  detectRepo,
  fallbackLocales,
  localeFiles,
  localeForFile,
  resolveCatalog,
  sourceFile,
  sourceLocale,
  translationsDir,
} from "./lib/adapters.mjs";
import { effectiveCatalog, loadCatalogBundles, translatedRendering } from "./lib/catalog.mjs";
import { sharedNamespace } from "./lib/keys.mjs";

export class LocalizationInputError extends Error {
  constructor(message) {
    super(message);
    this.name = "LocalizationInputError";
    this.exitCode = 2;
  }
}

const DEFAULT_THRESHOLD = 0.6;
const DEFAULT_LIMIT = 10;

// Apply one parsed argument to `options`, consuming its value from `rest` when
// the flag takes one. Kept as its own function (rather than a switch inline in
// the while loop below) so each case's `break` ends the switch, not the loop.
function applyArg(options, rest, a) {
  switch (a) {
    case "--all-siblings": {
      options.siblingLimit = Infinity;
      break;
    }
    case "--sibling-limit": {
      const value = rest.shift();
      if (!value || value.startsWith("--")) {
        throw new LocalizationInputError("--sibling-limit requires a value");
      }
      options.siblingLimit = Number(value);
      if (!Number.isSafeInteger(options.siblingLimit) || options.siblingLimit < 1) {
        throw new LocalizationInputError("--sibling-limit must be a positive integer");
      }
      break;
    }
    case "--fuzzy": {
      options.fuzzy = true;
      break;
    }
    case "--siblings": {
      options.siblings = true;
      break;
    }
    case "--prefix": {
      options.prefixes.push(rest.shift());
      break;
    }
    case "--out": {
      options.out = rest.shift();
      break;
    }
    case "--fallback-fuzzy": {
      options.fallbackFuzzy = true;
      break;
    }
    case "--report": {
      options.reportPath = rest.shift();
      break;
    }
    case "--threshold": {
      options.threshold = Number(rest.shift());
      break;
    }
    case "--limit": {
      options.limit = Number(rest.shift());
      break;
    }
    case "--locale": {
      options.locale = rest.shift();
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
    case "--json": {
      options.json = true;
      break;
    }
    default: {
      if (!a.startsWith("--")) options.queries.push(a);
    }
  }
}

export function parseArgs(argv) {
  const options = {
    queries: [],
    fuzzy: false,
    fallbackFuzzy: false,
    locale: null,
    threshold: DEFAULT_THRESHOLD,
    limit: DEFAULT_LIMIT,
    path: null,
    catalog: null,
    json: false,
    reportPath: null,
    siblings: false,
    siblingLimit: SIBLING_CAP,
    prefixes: [],
    out: null,
  };
  const rest = [...argv];
  while (rest.length > 0) {
    const a = rest.shift();
    applyArg(options, rest, a);
  }
  return options;
}

// Collapse a string to its comparable core: drop interpolation tokens (sprintf,
// ICU, Polyglot, MessageFormat) and markup tags so wording matches across
// syntaxes, lowercase, and collapse whitespace.
export function normalizeForMatch(text) {
  return String(text)
    .replaceAll(/%\([^)]*\)[a-z]/gi, " ") // %(name)s
    .replaceAll(/%\{[^}]*\}/g, " ") // %{name}
    .replaceAll(/%[a-z]/gi, " ") // %s, %d
    .replaceAll(/\{[^}]*\}/g, " ") // {name}, {0}
    .replaceAll(/<[^>]+>/g, " ") // <b>, </b>
    .toLowerCase()
    .replaceAll(/\s+/g, " ")
    .trim();
}

function bigrams(text) {
  const grams = new Map();
  for (let index = 0; index < text.length - 1; index += 1) {
    const gram = text.slice(index, index + 2);
    grams.set(gram, (grams.get(gram) ?? 0) + 1);
  }
  return grams;
}

// Sørensen–Dice coefficient over character bigrams: language-agnostic fuzzy
// string similarity in [0, 1], no dependencies. Strings under two characters
// have no bigrams, so fall back to exact equality.
export function diceCoefficient(a, b) {
  if (a === b) return 1;
  if (a.length < 2 || b.length < 2) return 0;
  const gramsA = bigrams(a);
  const gramsB = bigrams(b);
  let overlap = 0;
  let sizeA = 0;
  for (const count of gramsA.values()) sizeA += count;
  let sizeB = 0;
  for (const [gram, count] of gramsB.entries()) {
    sizeB += count;
    overlap += Math.min(count, gramsA.get(gram) ?? 0);
  }
  return (2 * overlap) / (sizeA + sizeB);
}

function similarity(query, value) {
  if (query === value) return 1;
  const a = normalizeForMatch(query);
  const b = normalizeForMatch(value);
  // Normalization intentionally removes case, placeholder syntax, and markup
  // for fuzzy retrieval. Those transformations can erase real semantic or
  // interpolation differences, so only byte-for-byte English equality earns
  // the exact score used by consensus and copyFrom.
  return Math.min(0.99, diceCoefficient(a, b));
}

// Source keys whose English value matches the query. Without fuzzy, only
// literal English matches (score 1) survive; with fuzzy, normalized similarity
// is capped below 1 and everything at or above the threshold is ranked.
export function findMatches(
  sourceFlat,
  query,
  { excludeKeys = new Set(), fuzzy = false, threshold = DEFAULT_THRESHOLD } = {},
) {
  const matches = [];
  for (const [key, source] of sourceFlat.entries()) {
    if (excludeKeys.has(key)) continue;
    const score = similarity(query, source);
    if (score === 1 || (fuzzy && score >= threshold)) matches.push({ key, source, score });
  }
  matches.sort((x, y) => y.score - x.score || x.key.localeCompare(y.key));
  return matches;
}

// Both untranslated.mjs report shapes carry { key, source } per row: the flat
// `entries` array (one row per locale × key) and the --group-by-key `groups`
// array (one row per key). Accept either, so one staged report serves both the
// authoring view and this lookup.
export function queriesFromReport(report) {
  const isGrouped = Boolean(report) && Array.isArray(report.groups);
  const rows = isGrouped ? report.groups : report?.entries;
  if (!report || !Array.isArray(rows)) {
    throw new LocalizationInputError("--report must contain an entries or groups array");
  }

  const label = isGrouped ? "groups" : "entries";
  const queries = [];
  const excludeKeys = new Set();
  const seenSources = new Set();
  const queryKeys = new Map();
  for (const [index, entry] of rows.entries()) {
    if (
      !entry ||
      typeof entry.source !== "string" ||
      typeof entry.key !== "string" ||
      entry.source.trim() === "" ||
      entry.key.trim() === ""
    ) {
      throw new LocalizationInputError(
        `--report ${label}[${index}] must contain non-empty source and key strings`,
      );
    }
    if (!seenSources.has(entry.source)) {
      queries.push(entry.source);
      seenSources.add(entry.source);
    }
    if (!queryKeys.has(entry.source)) queryKeys.set(entry.source, []);
    if (!queryKeys.get(entry.source).includes(entry.key)) {
      queryKeys.get(entry.source).push(entry.key);
    }
    excludeKeys.add(entry.key);
  }
  return { excludeKeys, queries, queryKeys: Object.fromEntries(queryKeys) };
}

function translationFingerprint(match) {
  return JSON.stringify([match.source, match.score, match.translations]);
}

// Equivalent catalog keys add no evidence when their English source, score, and
// complete locale rendering vector are identical. Keep one representative and
// retain the other key names as provenance.
export function dedupeEquivalentMatches(matches) {
  const byFingerprint = new Map();
  for (const match of matches) {
    const fingerprint = translationFingerprint(match);
    const existing = byFingerprint.get(fingerprint);
    if (existing) {
      (existing.equivalentKeys ??= []).push(match.key);
    } else {
      byFingerprint.set(fingerprint, { ...match });
    }
  }
  return byFingerprint.values().toArray();
}

// Per-locale rendering variants among the exact anchors, keyed by rendered
// value, each with the anchor keys that produced it. Kept as its own function
// (rather than a loop nested inside the per-locale loop below) so the
// `continue` only ever skips within this one loop.
function localeVariants(exact, locale) {
  const variants = new Map();
  for (const match of exact) {
    const value = match.translations[locale];
    if ([null, undefined, ""].includes(value)) continue;
    if (!variants.has(value)) variants.set(value, []);
    variants.get(value).push(match.key);
  }
  return variants;
}

// Exact English matches can disagree across shipped keys. Aggregate every exact
// anchor before applying the display cap so a first-match typo cannot masquerade
// as the canonical rendering.
export function exactConsensus(matches, targetLocales) {
  const exact = matches.filter((match) => match.score === 1);
  if (exact.length === 0) return null;
  const translations = {};
  const conflicts = [];
  let covered = 0;
  for (const locale of targetLocales) {
    const variants = localeVariants(exact, locale);
    const ranked = [...variants]
      .map(([value, keys]) => ({ value, count: keys.length, keys }))
      .sort((left, right) => right.count - left.count || left.value.localeCompare(right.value));
    if (ranked.length === 0) continue;
    covered += 1;
    translations[locale] = ranked[0].value;
    if (ranked.length > 1) conflicts.push({ locale, variants: ranked });
  }
  return {
    anchors: exact.length,
    covered,
    locales: targetLocales.length,
    translations,
    conflicts,
  };
}

// Namespace prefixes of the given keys: each key minus its last dotted segment.
export function namespacePrefixes(keys) {
  const prefixes = new Set();
  for (const key of keys) {
    const cut = key.lastIndexOf(".");
    if (cut > 0) prefixes.add(key.slice(0, cut));
  }
  return [...prefixes];
}

// Same-namespace evidence: for each requested prefix (a pending key's namespace under
// --siblings, or an explicit --prefix), every already-translated key under it with its
// source value and per-locale renderings. This is the authority-stack tier-0 evidence —
// established wording for the same component and element family — which a
// text-similarity lookup on the pending strings cannot surface. A key under two
// requested namespaces is reported once, in the most specific group; groups are capped
// so one giant namespace cannot flood the report.
const SIBLING_CAP = 25;

export function siblingGroups(
  catalog,
  sourceFlat,
  pendingKeys,
  prefixes,
  targetLocales,
  effectiveByLocale,
  siblingCap = SIBLING_CAP,
) {
  const ordered = [...new Set(prefixes)].sort((a, b) => b.length - a.length || a.localeCompare(b));
  const groups = new Map(ordered.map((prefix) => [prefix, []]));
  for (const [key, source] of sourceFlat.entries()) {
    if (pendingKeys.has(key)) continue;
    const prefix = ordered.find((candidate) => key.startsWith(`${candidate}.`));
    if (!prefix) continue;
    const translations = {};
    let isTranslated = false;
    for (const locale of targetLocales) {
      const value = translatedRendering(catalog, locale, key, effectiveByLocale.get(locale));
      translations[locale] = value;
      if (value !== null && value !== "") isTranslated = true;
    }
    // An untranslated sibling carries no rendering evidence.
    if (!isTranslated) continue;
    groups.get(prefix).push({ key, source, translations });
  }
  const cap = Number.isFinite(siblingCap) ? siblingCap : Infinity;
  return [...groups]
    .filter(([, keys]) => keys.length > 0)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([prefix, keys]) => {
      keys.sort((a, b) => a.key.localeCompare(b.key));
      if (keys.length <= cap) return { prefix, keys };
      return { prefix, keys: keys.slice(0, cap), omitted: keys.length - cap };
    });
}

export function readReport(reportPath) {
  let raw;
  try {
    raw = reportPath === "-" ? fs.readFileSync(0, "utf8") : fs.readFileSync(reportPath, "utf8");
  } catch (error) {
    throw new LocalizationInputError(`cannot read --report: ${error.message}`);
  }
  try {
    return queriesFromReport(JSON.parse(raw));
  } catch (error) {
    if (error instanceof LocalizationInputError) throw error;
    throw new LocalizationInputError(`cannot parse --report JSON: ${error.message}`);
  }
}

function validateEvidenceBundles(context, locales, bundles, errors) {
  const required = new Set([
    sourceLocale(context),
    ...locales.flatMap((locale) => fallbackLocales(context, locale)),
  ]);
  for (const locale of required) {
    const failure = errors.get(locale);
    if (failure) {
      throw new LocalizationInputError(`cannot read ${failure.file}: ${failure.error.message}`);
    }
    const bundle = bundles.get(locale);
    const duplicate = bundle?.parsed.duplicates[0];
    if (duplicate) {
      throw new LocalizationInputError(`duplicate key ${duplicate.key} in ${bundle.file}`);
    }
  }
}

export function run(options) {
  const prefixes = options.prefixes ?? [];
  if ((!options.queries || options.queries.length === 0) && prefixes.length === 0) {
    throw new LocalizationInputError('no english string given (e.g. "Email is required")');
  }
  if (options.siblings && !options.excludeKeys) {
    throw new LocalizationInputError("--siblings requires --report (the pending keys)");
  }
  const threshold = options.threshold ?? DEFAULT_THRESHOLD;
  if (!Number.isFinite(threshold) || threshold < 0 || threshold > 1) {
    throw new LocalizationInputError("--threshold must be between 0 and 1");
  }
  const limit = options.limit ?? DEFAULT_LIMIT;
  if (!Number.isSafeInteger(limit) || limit < 1) {
    throw new LocalizationInputError("--limit must be a positive integer");
  }
  const siblingLimit = options.siblingLimit ?? SIBLING_CAP;
  if (siblingLimit !== Infinity && (!Number.isSafeInteger(siblingLimit) || siblingLimit < 1)) {
    throw new LocalizationInputError("--sibling-limit must be a positive integer");
  }

  const repo = detectRepo(options.path || process.cwd());
  if (repo.repo === "unknown")
    throw new LocalizationInputError(`not a known i18n repo: ${repo.reason}`);
  const context = resolveCatalog(repo, options.catalog);
  if (context.error) throw new LocalizationInputError(context.error);

  const dir = translationsDir(context);
  let files;
  try {
    files = localeFiles(context);
  } catch (error) {
    throw new LocalizationInputError(`cannot read ${dir}: ${error.message}`);
  }
  const sourceFile_ = sourceFile(context);
  if (!files.includes(sourceFile_)) {
    throw new LocalizationInputError(`source file ${sourceFile_} not found in ${dir}`);
  }
  const sourceLocale_ = sourceLocale(context);

  const { bundles, errors } = loadCatalogBundles(context, dir, files);
  const sourceError = errors.get(sourceLocale_);
  if (sourceError) {
    throw new LocalizationInputError(`cannot read ${sourceFile_}: ${sourceError.error.message}`);
  }
  const sourceFlat = bundles.get(sourceLocale_).parsed.values;

  let targetLocales = files
    .filter((file) => file !== sourceFile_)
    .map((file) => localeForFile(context, file));
  if (options.locale) {
    targetLocales = targetLocales.filter((locale) => locale === options.locale);
    if (targetLocales.length === 0)
      throw new LocalizationInputError(`locale ${options.locale} not found`);
  }

  // Effective (post-fallback) values per reported locale, built once and reused
  // across every matched key.
  validateEvidenceBundles(context, targetLocales, bundles, errors);
  const effectiveByLocale = new Map(
    targetLocales.map((locale) => [locale, effectiveCatalog(context, locale, bundles)]),
  );

  const results = (options.queries ?? []).map((query) => {
    const excludeKeys = options.excludeKeys ?? new Set();
    let rawMatches;
    if (options.fallbackFuzzy) {
      const exact = findMatches(sourceFlat, query, { excludeKeys });
      rawMatches =
        exact.length > 0
          ? exact
          : findMatches(sourceFlat, query, { excludeKeys, fuzzy: true, threshold });
    } else {
      rawMatches = findMatches(sourceFlat, query, {
        excludeKeys,
        fuzzy: options.fuzzy,
        threshold,
      });
    }
    const enriched = rawMatches.map(({ key, source, score }) => {
      const translations = {};
      for (const locale of targetLocales) {
        translations[locale] = translatedRendering(
          context,
          locale,
          key,
          effectiveByLocale.get(locale),
        );
      }
      return { key, source, score, translations };
    });
    const consensus = exactConsensus(enriched, targetLocales);
    return {
      query,
      pendingKeys: options.queryKeys?.[query] ?? [],
      ...(consensus && { exactConsensus: consensus }),
      matches: dedupeEquivalentMatches(enriched).slice(0, limit),
    };
  });

  const report = { repo: context.repo, catalog: context.id, results };
  if (options.siblings || prefixes.length > 0) {
    const pending = options.excludeKeys ?? new Set();
    const requested = [...prefixes, ...(options.siblings ? namespacePrefixes(pending) : [])];
    report.siblings = siblingGroups(
      context,
      sourceFlat,
      pending,
      requested,
      targetLocales,
      effectiveByLocale,
      siblingLimit,
    );
  }
  return report;
}

export function formatHuman(report) {
  const lines = [];
  for (const { query, matches } of report.results) {
    lines.push(`## ${JSON.stringify(query)}`);
    if (matches.length === 0) {
      lines.push("  no existing English match — author fresh from the source and UI context.", "");
      continue;
    }
    for (const m of matches) {
      const score = m.score === 1 ? "exact" : m.score.toFixed(2);
      lines.push(`  ${m.key} [${score}] — EN: ${JSON.stringify(m.source)}`);
      for (const [locale, value] of Object.entries(m.translations)) {
        lines.push(`      ${locale}: ${value === null ? "(untranslated)" : JSON.stringify(value)}`);
      }
    }
    lines.push("");
  }
  const reportSiblings = report.siblings ?? [];
  for (const group of reportSiblings) {
    lines.push(`## siblings of ${group.prefix}.* (already translated)`);
    for (const sibling of group.keys) {
      lines.push(`  ${sibling.key} — EN: ${JSON.stringify(sibling.source)}`);
      for (const [locale, value] of Object.entries(sibling.translations)) {
        if (value !== null) lines.push(`      ${locale}: ${JSON.stringify(value)}`);
      }
    }
    if (group.omitted) {
      lines.push(
        `  (+${group.omitted} more sibling keys under this prefix; retrieve with --prefix "${group.prefix}" --all-siblings)`,
      );
    }
    lines.push("");
  }
  return lines.join("\n").trimEnd();
}

function compactResultLabel(result, namespace) {
  if (result.pendingKeys?.length > 0) {
    return result.pendingKeys
      .map((key) =>
        namespace && key.startsWith(`${namespace}.`) ? key.slice(namespace.length + 1) : key,
      )
      .join(", ");
  }
  return JSON.stringify(result.query);
}

function formatConsensusConflicts(consensus) {
  return consensus.conflicts.map(({ locale, variants }) => {
    const detail = variants
      .map((variant) => `${JSON.stringify(variant.value)} ×${variant.count}`)
      .join(" | ");
    return `    ${locale}: ${detail}`;
  });
}

// Compact view for the conversation when --out stages the full evidence. The
// scope already printed every English source, so this view identifies pending
// keys, useful anchors, and exact-match conflicts without repeating the source
// text or complete locale vectors.
export function formatTriage(report) {
  const pendingKeys = report.results.flatMap((result) => result.pendingKeys ?? []);
  const namespace = sharedNamespace(pendingKeys);
  const exact = report.results.filter((result) => result.exactConsensus);
  const fuzzy = report.results.filter(
    (result) => !result.exactConsensus && result.matches.length > 0,
  );
  const fresh = report.results.filter(
    (result) => !result.exactConsensus && result.matches.length === 0,
  );
  const keyCount = pendingKeys.length || report.results.length;
  const lines = [
    `evidence: ${keyCount} pending key(s): ${exact.length} exact, ${fuzzy.length} fuzzy, ${fresh.length} fresh`,
    ...(namespace ? [`  namespace: ${namespace}.*`] : []),
  ];
  if (exact.length > 0) {
    lines.push("  exact:");
    for (const result of exact) {
      const match = result.matches[0];
      const equivalent = match?.equivalentKeys?.length
        ? ` (+${match.equivalentKeys.length} equivalent key(s))`
        : "";
      const consensus = result.exactConsensus;
      lines.push(
        `    ${compactResultLabel(result, namespace)} ← ${match?.key ?? "(no translated anchor)"}${equivalent}; ${consensus.covered}/${consensus.locales} locale(s), ${consensus.conflicts.length} conflict(s)`,
      );
      lines.push(...formatConsensusConflicts(consensus));
    }
  }
  if (fuzzy.length > 0) {
    lines.push("  fuzzy:");
    for (const result of fuzzy) {
      const anchors = result.matches
        .map((match) => `${match.key} [${match.score.toFixed(2)}]`)
        .join(", ");
      lines.push(`    ${compactResultLabel(result, namespace)} ~ ${anchors}`);
    }
  }
  if (fresh.length > 0) {
    lines.push(
      `  fresh: ${fresh.map((result) => compactResultLabel(result, namespace)).join(", ")}`,
    );
  }
  const reportSiblings = report.siblings ?? [];
  for (const group of reportSiblings) {
    const omitted = group.omitted
      ? `, +${group.omitted} omitted (retrieve with --prefix "${group.prefix}" --all-siblings)`
      : "";
    lines.push(
      `## siblings of ${group.prefix}.* (${group.keys.length} translated key(s)${omitted})`,
    );
    for (const sibling of group.keys) {
      lines.push(
        `  ${sibling.key.slice(group.prefix.length + 1)}: ${JSON.stringify(sibling.source)}`,
      );
    }
  }
  return lines.join("\n");
}

function main() {
  try {
    const options = parseArgs(process.argv.slice(2));
    if (options.reportPath) {
      if (options.queries.length > 0) {
        throw new LocalizationInputError("use either --report or explicit strings, not both");
      }
      Object.assign(options, readReport(options.reportPath));
      if (options.queries.length === 0 && options.prefixes.length === 0) {
        throw new LocalizationInputError(
          "--report contains no pending strings — nothing to look up",
        );
      }
    }
    const report = run(options);
    if (options.out) {
      fs.writeFileSync(options.out, `${JSON.stringify(report)}\n`);
      console.log(formatTriage(report));
    } else {
      console.log(options.json ? JSON.stringify(report) : formatHuman(report));
    }
  } catch (error) {
    console.error(`find-translations: ${error.message}`);
    process.exit(error.exitCode ?? 1);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}
