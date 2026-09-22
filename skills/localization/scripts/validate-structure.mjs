#!/usr/bin/env node
// L1 structural gate: every locale must reference the same interpolation
// placeholders and markup tags as the source, and must parse. Deterministic, no
// network or model. Exits non-zero on a gate failure so it can run in CI.
//
//   node validate-structure.mjs [--path <repo>] [--catalog <id>] [--locale <name>] [--branch | --since <ref>] [--show-pre-existing] [--strict] [--json]
//
// Gate failures: placeholder-mismatch, markup-mismatch, parse-error, duplicate-key.
// --strict additionally fails on stale keys (in a locale but not the source).
// Untranslated keys (in the source but not yet a locale) are reported without
// failing: they are the normal pending-translation state, not a defect.
//
// --json output shape: { ok, repo, catalog, syntax, sourceLocale, sourceFile,
// localeResults: [{ locale, file, stale, explicitUntranslated,
// effectiveUntranslated, inherited, issues: [{ key, code, ... }],
// parseError? }], totals } — findings live under localeResults[].issues, there
// is no top-level findings array. With --branch/--since the report adds
// `since`, `totals.branchIntroducedGateFailures`, and per-issue
// `branchRelevant`. Stale-key attribution is reported separately under
// `branchIntroducedStaleKeys`. Fatal config errors return { ok: false, fatal }.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { resolveBranchForkPoint } from "./lib/git-fork.mjs";
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
import { changedSourceKeys } from "./lib/branch-scope.mjs";
import {
  classifyAbsence,
  effectiveCatalog,
  loadCatalogBundles,
  parseCatalogBuffer,
  sourceAbsentAtReference,
} from "./lib/catalog.mjs";
import { diffSignature, signatureFor } from "./lib/placeholders.mjs";

const GATE_CODES = new Set([
  "placeholder-mismatch",
  "markup-mismatch",
  "parse-error",
  "duplicate-key",
]);

// Apply one parsed CLI token to `options`, consuming any following value token(s)
// from `rest` (e.g. `--path <dir>`). Throws for a malformed `--since`.
function applyArg(a, rest, options) {
  switch (a) {
    case "--json": {
      options.json = true;
      return;
    }
    case "--strict": {
      options.strict = true;
      return;
    }
    case "--path": {
      options.path = rest.shift();
      return;
    }
    case "--catalog": {
      options.catalog = rest.shift();
      return;
    }
    case "--locale": {
      {
        options.locale = rest.shift();
        // No default
      }
      return;
    }
    case "--branch": {
      options.branch = true;
      return;
    }
    case "--since": {
      const value = rest.shift();
      if (!value || value.startsWith("--")) {
        throw new Error("--since requires a ref value");
      }
      options.since = value;
      return;
    }
    case "--show-pre-existing": {
      options.showPreExisting = true;
      return;
    }
  }
}

export function parseArgs(argv) {
  const options = {
    json: false,
    strict: false,
    path: null,
    catalog: null,
    locale: null,
    branch: false,
    since: null,
    showPreExisting: false,
  };
  const rest = [...argv];
  while (rest.length > 0) {
    const a = rest.shift();
    applyArg(a, rest, options);
  }
  return options;
}

// Trimmed stdout, or null on a non-zero exit (so callers decide whether a missing ref is
// fatal). Throws only when git can't be spawned.
function gitText(root, arguments_) {
  const result = spawnSync("git", arguments_, { cwd: root, encoding: "utf8" });
  if (result.error) throw new Error(`git ${arguments_[0]} failed: ${result.error.message}`);
  return result.status === 0 ? result.stdout.trim() : null;
}

// The baseline to compare the branch against: an explicit --since ref, else the merge-base
// of HEAD and the default remote branch (mirrors untranslated.mjs's discovery baseline).
function resolveBaseline(context, options) {
  if (options.since) {
    return { commit: options.since, ref: options.since, kind: "explicit", warning: null };
  }
  let resolved;
  try {
    resolved = resolveBranchForkPoint(context.root);
  } catch (error) {
    throw new Error(`${error.message}; pass --since <ref>`, { cause: error });
  }
  return {
    commit: resolved.forkPoint,
    ref: resolved.remoteRef,
    kind: "fork-point",
    warning: resolved.warning,
  };
}

// The source catalog's flat key/value Map as it stood at `ref`, decoded by the catalog's
// own codec so a properties bundle still decodes from bytes.
function sourceValuesAtReference(context, reference) {
  const relativePath = path
    .relative(context.root, path.join(translationsDir(context), sourceFile(context)))
    .split(path.sep)
    .join("/");
  const result = spawnSync("git", ["show", `${reference}:${relativePath}`], { cwd: context.root });
  if (result.error)
    throw new Error(`cannot read the source at ${reference}: ${result.error.message}`);
  if (result.status !== 0) {
    if (sourceAbsentAtReference(context, reference)) return new Map();
    const detail = result.stderr.toString("utf8").trim() || `git show exited ${result.status}`;
    throw new Error(`cannot read the source at ${reference}: ${detail}`);
  }
  return parseCatalogBuffer(context, result.stdout).values;
}

// Catalog filenames touched on the branch since `ref` (including untracked locale files).
function touchedCatalogFiles(context, reference) {
  const diffNames =
    gitText(context.root, ["diff", "--name-only", reference, "--", context.dir]) || "";
  const untrackedNames =
    gitText(context.root, ["ls-files", "--others", "--exclude-standard", "--", context.dir]) || "";
  const names = `${diffNames}\n${untrackedNames}`;
  return new Set(
    names
      .split("\n")
      .filter(Boolean)
      .map((name) => name.split("/").pop()),
  );
}

// Per touched locale file, the keys the branch added or changed, from parsing the file
// at the baseline and comparing with the current bundle — key-precise for every catalog
// format, including nested JSON and TypeScript whose dotted keys are not literal in a
// diff. A file absent at the baseline diffs against an empty map (every key is new); a
// file whose baseline cannot be parsed maps to null, so its attribution falls back to
// file-touched.
function changedKeysByFile(context, baseline, files, bundles) {
  const bundleByFile = new Map(bundles.values().map((bundle) => [bundle.file, bundle]));
  const byFile = new Map();
  for (const file of files) {
    if (file === sourceFile(context)) continue; // source keys are diffed via sourceValuesAtRef
    const bundle = bundleByFile.get(file);
    if (!bundle) continue; // deleted or currently unparseable: no per-key issues to attribute
    const relativePath = path
      .relative(context.root, path.join(translationsDir(context), file))
      .split(path.sep)
      .join("/");
    const shown = spawnSync("git", ["show", `${baseline}:${relativePath}`], { cwd: context.root });
    if (shown.error) throw new Error(`cannot read ${file} at ${baseline}: ${shown.error.message}`);
    let base;
    if (shown.status === 0) {
      try {
        base = parseCatalogBuffer(context, shown.stdout);
      } catch {
        byFile.set(file, null);
        continue;
      }
    } else {
      base = { values: new Map(), duplicates: [] };
    }
    const changed = changedSourceKeys(bundle.parsed.values, base.values);
    // A key duplicated now but not at the baseline is branch-introduced even when the
    // parsed value survived unchanged.
    const baseDuplicates = new Set((base.duplicates ?? []).map((duplicate) => duplicate.key));
    const currentDuplicates = bundle.parsed.duplicates ?? [];
    for (const duplicate of currentDuplicates) {
      if (!baseDuplicates.has(duplicate.key)) changed.add(duplicate.key);
    }
    byFile.set(file, changed);
  }
  return byFile;
}

// The set of branch-changed source keys, the touched catalog filenames, and each touched
// locale file's own branch-changed keys. A per-key finding is branch-introduced when its
// key changed in the source or in its owning locale file; whole-file parse errors (and
// files whose baseline cannot be key-diffed) attribute by the file being touched. The
// skill's own apply step touches every locale file, so file-touched alone would label
// every pre-existing failure branch-introduced right after a sanctioned write. Returns
// null when the run isn't branch-scoped.
export function resolveBranchScope(context, options, sourceValues, bundles) {
  if (!options.branch && !options.since) return null;
  const baseline = resolveBaseline(context, options);
  const keys = changedSourceKeys(sourceValues, sourceValuesAtReference(context, baseline.commit));
  const files = touchedCatalogFiles(context, baseline.commit);
  const localeKeys = changedKeysByFile(context, baseline.commit, files, bundles);
  return {
    baseline: baseline.commit,
    baselineRef: baseline.ref,
    baselineKind: baseline.kind,
    baselineWarning: baseline.warning,
    keys,
    files,
    localeKeys,
  };
}

export function signaturesOf(syntax, flat) {
  const sigs = new Map();
  for (const [key, value] of flat.entries()) sigs.set(key, signatureFor(syntax, value));
  return sigs;
}

// Parse errors in the source itself; must be fixed before locales mean anything.
export function sourceParseErrors(sourceSigs) {
  const errors = [];
  for (const [key, sig] of sourceSigs.entries()) {
    if (sig.error) errors.push({ key, detail: sig.error });
  }
  return errors;
}

// Compare one locale's signatures against the source's.
export function compareSignatures(sourceSigs, localeSigs, syntax) {
  const issues = [];
  const stale = [];
  let untranslated = 0;

  for (const [key, sig] of localeSigs.entries()) {
    if (!sourceSigs.has(key)) {
      stale.push(key);
      continue;
    }
    if (sig.error) {
      // A parse failure leaves the signature unreliable, so stop here rather than
      // also reporting placeholder/markup mismatches derived from the partial parse.
      issues.push({ key, code: "parse-error", detail: sig.error });
      continue;
    }
    const source = sourceSigs.get(key);
    const ph = diffSignature(source.placeholders, sig.placeholders, { syntax });
    if (ph.missing.length > 0 || ph.extra.length > 0) {
      issues.push({
        key,
        code: "placeholder-mismatch",
        missing: ph.missing,
        extra: ph.extra,
        sourceSig: source.placeholders,
        localeSig: sig.placeholders,
      });
    }
    const mk = diffSignature(source.markup, sig.markup);
    if (mk.missing.length > 0 || mk.extra.length > 0) {
      issues.push({
        key,
        code: "markup-mismatch",
        missing: mk.missing,
        extra: mk.extra,
        sourceSig: source.markup,
        localeSig: sig.markup,
      });
    }
  }

  for (const key of sourceSigs.keys()) {
    if (!localeSigs.has(key)) untranslated += 1;
  }

  return { stale, untranslated, issues };
}

function run(options) {
  const repo = detectRepo(options.path || process.cwd());
  if (repo.repo === "unknown") {
    return { ok: false, fatal: `not a known i18n repo: ${repo.reason}` };
  }
  const context = resolveCatalog(repo, options.catalog);
  if (context.error) {
    return { ok: false, fatal: context.error };
  }
  if (options.branch && options.since) {
    return { ok: false, fatal: "--branch and --since are mutually exclusive" };
  }

  const dir = translationsDir(context);
  let files;
  try {
    // deepcode ignore: directory comes from the detected repository, not untrusted input; local run with the user's own permissions.
    files = localeFiles(context);
  } catch (error) {
    return { ok: false, fatal: `cannot read ${dir}: ${error.message}` };
  }
  const sourceFile_ = sourceFile(context);
  if (!files.includes(sourceFile_)) {
    return { ok: false, fatal: `source file ${sourceFile_} not found in ${dir}` };
  }

  const sourceLocale_ = sourceLocale(context);
  const { bundles, errors } = loadCatalogBundles(context, dir, files);
  const sourceError = errors.get(sourceLocale_);
  if (sourceError) {
    return { ok: false, fatal: `cannot read ${sourceFile_}: ${sourceError.error.message}` };
  }
  const sourceParsed = bundles.get(sourceLocale_).parsed;
  const sourceSigs = signaturesOf(context.syntax, sourceParsed.values);

  let branchScope;
  try {
    branchScope = resolveBranchScope(context, options, sourceParsed.values, bundles);
  } catch (error) {
    return { ok: false, fatal: `branch scope: ${error.message}` };
  }

  const sourceErrors = [
    ...sourceParsed.duplicates.map((duplicate) => ({
      key: duplicate.key,
      code: "duplicate-key",
      detail: `duplicate key at lines ${duplicate.startLine}-${duplicate.endLine}`,
    })),
    ...sourceParseErrors(sourceSigs).map((issue) => ({ ...issue, code: "parse-error" })),
  ];
  if (sourceErrors.length > 0) {
    return {
      ok: false,
      fatal: `${sourceFile_} has ${sourceErrors.length} invalid entry or entries`,
      srcErrors: sourceErrors,
    };
  }

  let targets = files
    .filter((file) => file !== sourceFile_)
    .map((file) => ({ file, locale: localeForFile(context, file) }));
  if (options.locale) {
    targets = targets.filter(
      (target) => target.file === options.locale || target.locale === options.locale,
    );
    if (targets.length === 0) return { ok: false, fatal: `locale ${options.locale} not found` };
  }

  const localeResults = targets.map(({ file, locale }) => {
    const parseFailure = errors.get(locale);
    if (parseFailure) {
      return {
        locale,
        file,
        parseError: parseFailure.error.message,
        stale: [],
        explicitUntranslated: 0,
        effectiveUntranslated: 0,
        inherited: 0,
        issues: [],
      };
    }

    const fallbackFailure = fallbackLocales(context, locale).find((candidate) =>
      errors.has(candidate),
    );
    if (fallbackFailure) {
      const failure = errors.get(fallbackFailure);
      return {
        locale,
        file: failure.file,
        parseError: `fallback file ${failure.file}: ${failure.error.message}`,
        stale: [],
        explicitUntranslated: 0,
        effectiveUntranslated: 0,
        inherited: 0,
        issues: [],
      };
    }

    const direct = bundles.get(locale).parsed;
    const effective = effectiveCatalog(context, locale, bundles);
    const localeSigs = signaturesOf(context.syntax, effective.values);
    const comparison = compareSignatures(sourceSigs, localeSigs, context.syntax);
    const stale = direct.values
      .keys()
      .filter((key) => !sourceSigs.has(key))
      .toArray();
    let explicitUntranslated = 0;
    let effectiveUntranslated = 0;
    let inherited = 0;
    for (const key of sourceSigs.keys()) {
      if (direct.values.has(key)) continue;
      explicitUntranslated += 1;
      const status = classifyAbsence(context, locale, key, effective);
      if (status.inherited) inherited += 1;
      if (status.needsTranslation) effectiveUntranslated += 1;
    }

    const duplicateIssues = direct.duplicates.map((duplicate) => ({
      key: duplicate.key,
      code: "duplicate-key",
      detail: `duplicate key at lines ${duplicate.startLine}-${duplicate.endLine}`,
      ownedBy: locale,
      file,
    }));
    const signatureIssues = comparison.issues.map((issue) => {
      const owner = effective.owners.get(issue.key);
      return owner ? { ...issue, ownedBy: owner.locale, file: owner.file } : issue;
    });

    return {
      locale,
      file,
      stale,
      explicitUntranslated,
      effectiveUntranslated,
      inherited,
      issues: [...duplicateIssues, ...signatureIssues],
    };
  });

  // In branch mode, label each finding as introduced by this branch or pre-existing, so the
  // report distinguishes the two without the caller hand-diffing every flagged key.
  if (branchScope) {
    for (const result of localeResults) {
      if (result.parseError) {
        result.branchRelevant = branchScope.files.has(result.file);
      }
      const fileKeys = branchScope.localeKeys.get(result.file);
      result.branchRelevantStale = result.stale.filter(
        (key) =>
          branchScope.keys.has(key) ||
          (fileKeys === null ? branchScope.files.has(result.file) : Boolean(fileKeys?.has(key))),
      );
      for (const issue of result.issues) {
        const file = issue.file ?? result.file;
        const issueFileKeys = branchScope.localeKeys.get(file);
        issue.branchRelevant =
          branchScope.keys.has(issue.key) ||
          (issueFileKeys === null
            ? branchScope.files.has(file)
            : Boolean(issueFileKeys?.has(issue.key)));
      }
    }
  }

  const gateFailures = localeResults.reduce(
    (sum, result) =>
      sum +
      result.issues.filter((issue) => GATE_CODES.has(issue.code)).length +
      (result.parseError ? 1 : 0),
    0,
  );
  const staleTotal = localeResults.reduce((sum, r) => sum + r.stale.length, 0);
  const explicitUntranslatedTotal = localeResults.reduce(
    (sum, result) => sum + result.explicitUntranslated,
    0,
  );
  const effectiveUntranslatedTotal = localeResults.reduce(
    (sum, result) => sum + result.effectiveUntranslated,
    0,
  );
  const inheritedTotal = localeResults.reduce((sum, result) => sum + result.inherited, 0);

  const isOk = gateFailures === 0 && (!options.strict || staleTotal === 0);
  const totals = {
    gateFailures,
    staleTotal,
    explicitUntranslatedTotal,
    effectiveUntranslatedTotal,
    inheritedTotal,
    locales: localeResults.length,
  };
  const report = {
    ok: isOk,
    repo: context.repo,
    catalog: context.id,
    syntax: context.syntax,
    sourceLocale: sourceLocale_,
    sourceFile: sourceFile_,
    localeResults,
    totals,
  };
  if (branchScope) {
    report.since = branchScope.baseline;
    report.scopeRef = branchScope.baselineRef;
    report.scopeKind = branchScope.baselineKind;
    if (branchScope.baselineWarning) report.scopeWarning = branchScope.baselineWarning;
    totals.branchIntroducedGateFailures = localeResults.reduce(
      (sum, result) =>
        sum +
        result.issues.filter((issue) => GATE_CODES.has(issue.code) && issue.branchRelevant).length +
        (result.parseError && result.branchRelevant ? 1 : 0),
      0,
    );
    totals.branchIntroducedStaleKeys = localeResults.reduce(
      (sum, result) => sum + result.branchRelevantStale.length,
      0,
    );
    if (options.strict) {
      totals.branchIntroducedStrictFailures =
        totals.branchIntroducedGateFailures + totals.branchIntroducedStaleKeys;
    }
  }
  return report;
}

const EXAMPLE_CAP = 20;

export function formatHuman(report, { showPreExisting = false } = {}) {
  if (report.fatal) {
    const lines = [`localization: ${report.fatal}`];
    const srcErrorExamples = (report.srcErrors || []).slice(0, EXAMPLE_CAP);
    for (const e of srcErrorExamples) lines.push(`  ${e.key}: ${e.detail}`);
    return lines.join("\n");
  }

  const t = report.totals;
  const isBranchScoped = report.since !== undefined;
  const lines = [
    `localization: ${report.repo} (${report.syntax}): ${t.locales} locales vs ${report.sourceLocale}`,
    "",
    `  ${t.gateFailures === 0 ? "✓" : "✗"} ${t.gateFailures} gate failure(s) (placeholder/markup/parse/duplicate)`,
  ];
  if (isBranchScoped) {
    const baseline =
      report.scopeKind === "fork-point"
        ? `fork point ${report.scopeRef} @ ${report.since.slice(0, 12)}`
        : (report.scopeRef ?? report.since.slice(0, 12));
    lines.push(
      `      ↳ ${t.branchIntroducedGateFailures} introduced by this branch since ${baseline}; ${t.gateFailures - t.branchIntroducedGateFailures} pre-existing`,
    );
    if (report.scopeWarning) lines.push(`      ↳ ${report.scopeWarning}`);
  }
  lines.push(`  ⚠ ${t.staleTotal} stale key(s) (in a locale, not in source)`);
  if (isBranchScoped) {
    lines.push(
      `      ↳ ${t.branchIntroducedStaleKeys} introduced by this branch; ${t.staleTotal - t.branchIntroducedStaleKeys} pre-existing`,
    );
  }
  lines.push(
    `  · ${t.explicitUntranslatedTotal} explicit untranslated key(s)`,
    `  · ${t.effectiveUntranslatedTotal} effective untranslated key(s) after fallback`,
    `  · ${t.inheritedTotal} inherited translation(s)`,
  );

  const allExamples = [];
  for (const r of report.localeResults) {
    if (r.parseError) {
      allExamples.push({
        locale: r.locale,
        file: r.file,
        code: "parse-error",
        detail: r.parseError,
        branchRelevant: r.branchRelevant,
      });
    }
    const gateIssues = r.issues.filter((index_) => GATE_CODES.has(index_.code));
    for (const index of gateIssues) allExamples.push({ locale: r.locale, ...index });
  }

  const examples =
    isBranchScoped && !showPreExisting
      ? allExamples.filter((example) => example.branchRelevant)
      : allExamples;
  const hiddenPreExisting = allExamples.length - examples.length;
  if (examples.length > 0 || hiddenPreExisting > 0) {
    lines.push("");
    for (const e of examples.slice(0, EXAMPLE_CAP)) {
      const owner = e.file ? `; ${e.file}` : "";
      const tag = isBranchScoped ? `[${e.branchRelevant ? "branch" : "pre-existing"}] ` : "";
      lines.push(`  ${tag}[${e.code}] ${e.key ?? ""}  (${e.locale}${owner})`);
      if (e.code === "placeholder-mismatch" || e.code === "markup-mismatch") {
        lines.push(`      source: {${e.sourceSig.join(", ")}}`);
        lines.push(`      locale: {${e.localeSig.join(", ")}}`);
        if (e.missing.length > 0) lines.push(`      missing: ${e.missing.join(", ")}`);
        if (e.extra.length > 0) lines.push(`      extra: ${e.extra.join(", ")}`);
      } else if (e.detail) {
        lines.push(`      ${e.detail}`);
      }
    }
    if (examples.length > EXAMPLE_CAP) {
      lines.push(
        `  (+${examples.length - EXAMPLE_CAP} more gate failures; run with --json for the full list)`,
      );
    }
    if (hiddenPreExisting > 0) {
      lines.push(
        `  (+${hiddenPreExisting} pre-existing gate failure(s) hidden; use --show-pre-existing or --json to inspect them)`,
      );
    }
  }

  return lines.join("\n");
}

function main() {
  const argv = process.argv.slice(2);
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    const report = { ok: false, fatal: error.message };
    console.log(argv.includes("--json") ? JSON.stringify(report) : formatHuman(report));
    process.exit(2);
  }
  const report = run(options);

  if (options.json) {
    console.log(JSON.stringify(report));
  } else {
    console.log(formatHuman(report, { showPreExisting: options.showPreExisting }));
  }

  if (report.fatal) process.exit(2);
  process.exit(report.ok ? 0 : 1);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}

// Re-exported so the test suite imports the full structural surface from one module.
export { flatten } from "./lib/flatten.mjs";
