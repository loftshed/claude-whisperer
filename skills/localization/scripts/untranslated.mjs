#!/usr/bin/env node
// List source strings without a valid effective translation in target locales.
// With --branch, restrict the list to source keys added or changed since the
// immutable fork point with the default remote branch; --since <ref> picks an
// explicit comparison commit instead. Without either, report the full set.
//
//   node untranslated.mjs [--path <repo>] [--catalog <id>] [--locale <name>] [--branch | --since <ref>] [--exclude-key <key> ...] [--restage-key <key> ...] [--audit-source] [--group-by-key] [--json | --out <file>]
//
// --json output shape: { repo, catalog, sourceFile, since, inherited, totals,
// entries: [{ catalog, locale, key, source, effectiveFrom? }] } — or, with
// --group-by-key, a report-level locales array naming the full target set plus
// a groups array ({ key, source, locales?, replaceExisting?,
// replaceExistingLocales?, effectiveFrom? }) replacing entries;
// the report's single catalog is stated once at report level, and a group
// carries its own locales only when it deviates from the full set (e.g.
// already translated in some locales).
//
// --out <file> writes that JSON report to <file> and prints the human summary
// to stdout — one run stages the machine report and shows the compact view,
// with no second invocation and nothing oversized entering the conversation.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { resolveBranchForkPoint } from "./lib/git-fork.mjs";
import { classifyUsageResult, run as findUsages, newlyUsedMessageKeys } from "./find-usages.mjs";
import {
  detectRepo,
  localeFiles,
  localeForFile,
  resolveCatalog,
  sourceFile,
  translationsDir,
} from "./lib/adapters.mjs";
import { changedSourceKeys, newlyUsedKeys } from "./lib/branch-scope.mjs";
import {
  classifyAbsence,
  effectiveCatalog,
  loadCatalogBundles,
  parseCatalogBuffer,
  sourceAbsentAtReference,
} from "./lib/catalog.mjs";
import { keyOccursAsDynamicPrefix, keyOccursAsToken } from "./lib/usage.mjs";

// Re-exported so callers (and the test suite) keep importing the branch-scope helpers
// from this module's public surface.
export { changedSourceKeys, newlyUsedKeys } from "./lib/branch-scope.mjs";

export class LocalizationInputError extends Error {
  constructor(message) {
    super(message);
    this.name = "LocalizationInputError";
    this.exitCode = 2;
  }
}

export const USAGE = `Usage: node untranslated.mjs [options]

Discover untranslated source keys, optionally scoped to the current branch.

Options:
  --branch                 Diff from the immutable fork point with the cached remote default
  --since <ref>            Diff from an explicit comparison ref
  --exclude-key <key>      Exclude one audited source key (repeatable)
  --restage-key <key>      Re-open one key this branch already translated (repeatable)
  --audit-source           Classify pending keys by render usage
  --group-by-key           Emit one discovery row per source key
  --locale <name>          Limit discovery to one target locale
  --path <repo>            Consumer repository path
  --catalog <id>           Catalog id when the repository has more than one
  --out <file>             Stage JSON and print the compact human summary
  --json                   Print JSON instead of the human summary
  --help                   Show this help`;

function takeValue(rest, flag) {
  const value = rest.shift();
  if (!value || value.startsWith("--")) {
    throw new LocalizationInputError(`${flag} requires a value`);
  }
  return value;
}

// Applies one shifted-off argument to `options` (mutated in place), consuming
// its value from `rest` when it takes one. Kept as its own function so the
// per-argument `switch` isn't nested inside the `parseArgs` loop.
function applyArgument(argument, rest, options) {
  switch (argument) {
    case "--json": {
      options.json = true;
      break;
    }
    case "--group-by-key": {
      options.groupByKey = true;
      break;
    }
    case "--out": {
      options.out = takeValue(rest, argument);
      break;
    }
    case "--exclude-key": {
      options.excludeKeys.push(takeValue(rest, argument));
      break;
    }
    case "--restage-key": {
      options.restageKeys.push(takeValue(rest, argument));
      break;
    }
    case "--path": {
      options.path = takeValue(rest, argument);
      break;
    }
    case "--catalog": {
      options.catalog = takeValue(rest, argument);
      break;
    }
    case "--locale": {
      options.locale = takeValue(rest, argument);
      break;
    }
    case "--since": {
      options.since = takeValue(rest, argument);
      break;
    }
    case "--branch": {
      options.branch = true;
      break;
    }
    case "--audit-source": {
      options.auditSource = true;
      break;
    }
    case "--help": {
      options.help = true;
      break;
    }
    default: {
      throw new LocalizationInputError(`unknown argument ${argument}`);
    }
  }
}

export function parseArgs(argv) {
  const options = {
    json: false,
    path: null,
    catalog: null,
    locale: null,
    since: null,
    branch: false,
    auditSource: false,
    groupByKey: false,
    out: null,
    excludeKeys: [],
    restageKeys: [],
    help: false,
  };
  const rest = [...argv];
  while (rest.length > 0) {
    applyArgument(rest.shift(), rest, options);
  }
  if (options.branch && options.since) {
    throw new LocalizationInputError("--branch and --since are mutually exclusive");
  }
  options.excludeKeys = [...new Set(options.excludeKeys)];
  options.restageKeys = [...new Set(options.restageKeys)];
  const contradictory = options.restageKeys.filter((key) => options.excludeKeys.includes(key));
  if (contradictory.length > 0) {
    throw new LocalizationInputError(
      `cannot both exclude and restage: ${contradictory.join(", ")}`,
    );
  }
  return options;
}

// Decode file content through the catalog's codec into a flat key/value Map.
// Properties content decodes from bytes (fatal UTF-8, then ISO-8859-1), so the
// buffer is never read as utf8 up front.
function flatValues(context, buffer, label) {
  try {
    return parseCatalogBuffer(context, buffer).values;
  } catch (error) {
    throw new LocalizationInputError(`cannot parse ${label}: ${error.message}`);
  }
}

function loadFlat(context, dir, file) {
  let buffer;
  try {
    buffer = fs.readFileSync(path.join(dir, file));
  } catch (error) {
    throw new LocalizationInputError(`cannot read ${file}: ${error.message}`);
  }
  return flatValues(context, buffer, file);
}

export function resolveMergeBase(context) {
  try {
    return resolveBranchForkPoint(context.root).forkPoint;
  } catch (error) {
    throw new LocalizationInputError(error.message);
  }
}

function catalogAtReference(context, reference, file, { required = false } = {}) {
  const catalogPath = path.join(translationsDir(context), file);
  const relativePath = path.relative(context.root, catalogPath).split(path.sep).join("/");
  // No encoding option: stdout stays a Buffer so the codec decides how the
  // bytes decode.
  const result = spawnSync("git", ["show", `${reference}:${relativePath}`], { cwd: context.root });

  if (result.error) {
    throw new LocalizationInputError(
      `cannot resolve --since ${reference}: ${result.error.message}`,
    );
  }
  if (result.status !== 0) {
    if (!required || sourceAbsentAtReference(context, reference)) return new Map();
    const detail = result.stderr.toString("utf8").trim() || `git show exited ${result.status}`;
    throw new LocalizationInputError(`cannot resolve --since ${reference}: ${detail}`);
  }

  return flatValues(context, result.stdout, `${file} at ${reference}`);
}

function sourceAtReference(context, reference) {
  return catalogAtReference(context, reference, sourceFile(context), { required: true });
}

function outputPathInRepo(context, outputPath) {
  if (!outputPath) return null;
  const absolute = path.resolve(process.cwd(), outputPath);
  const relative = path.relative(context.root, absolute);
  if (
    !relative ||
    relative === ".." ||
    relative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relative)
  ) {
    return null;
  }
  return relative.split(path.sep).join("/");
}

function diffTargetPath(header) {
  // Unquoted paths containing spaces carry a trailing tab separator. Literal
  // tabs inside filenames are escaped within Git's quoted path instead.
  const encoded = header.split("\t", 1)[0];
  const quoted = encoded.startsWith('"');
  const name = quoted
    ? encoded.slice(1, -1).replaceAll(/\\([0-7]{1,3}|.)/g, (_, escape) => {
        if (/^[0-7]/.test(escape)) return String.fromCodePoint(Number.parseInt(escape, 8));
        return (
          { a: "\u{7}", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t", v: "\v" }[escape] ?? escape
        );
      })
    : encoded;
  return name === "/dev/null" ? null : name.slice(2);
}

function addedDiffSources(diff) {
  const sources = [];
  let current = null;
  let isInHunk = false;
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git ")) {
      current = null;
      isInHunk = false;
    } else if (!isInHunk && line.startsWith("+++ ")) {
      const file = diffTargetPath(line.slice(4));
      if (file !== null) {
        current = { file, addedLines: [] };
        sources.push(current);
      }
    } else if (line.startsWith("@@ ")) {
      isInHunk = true;
    } else if (isInHunk && current && line.startsWith("+")) {
      current.addedLines.push(line.slice(1));
    }
  }
  return sources.map(({ file, addedLines }) => ({ file, addedText: addedLines.join("\n") }));
}

// Keep additions grouped by their current file so descriptor members can be
// resolved through that file's imports. The diff includes committed and local
// changes; every line in an untracked file counts as an addition.
function addedSourcesSince(context, reference, ignoredFiles = []) {
  const ignored = new Set(ignoredFiles.filter(Boolean));
  const pathspec = [
    ".",
    ...(context.catalogDirs ?? [context.dir]).map((dir) => `:(exclude,literal)${dir}`),
    ...[...ignored].map((file) => `:(exclude,literal)${file}`),
  ];
  const result = spawnSync(
    "git",
    [
      "-c",
      "core.quotepath=false",
      "diff",
      "--no-color",
      "--no-ext-diff",
      "--no-textconv",
      "--unified=0",
      "--src-prefix=a/",
      "--dst-prefix=b/",
      reference,
      "--",
      ...pathspec,
    ],
    {
      cwd: context.root,
      encoding: "utf8",
    },
  );
  if (result.error) {
    throw new LocalizationInputError(`git diff failed: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new LocalizationInputError(result.stderr.trim() || `git diff exited ${result.status}`);
  }
  const added = addedDiffSources(result.stdout);

  const untracked = spawnSync(
    "git",
    ["ls-files", "--others", "--exclude-standard", "-z", "--", ...pathspec],
    { cwd: context.root, encoding: "utf8" },
  );
  if (untracked.error || untracked.status !== 0) {
    throw new LocalizationInputError(
      untracked.error?.message ||
        untracked.stderr.trim() ||
        `git ls-files exited ${untracked.status}`,
    );
  }
  const untrackedFiles = untracked.stdout.split("\0").filter(Boolean);
  for (const file of untrackedFiles) {
    if (ignored.has(file.split(path.sep).join("/"))) continue;
    try {
      const source = fs.readFileSync(path.join(context.root, file), "utf8");
      if (!source.includes("\0")) added.push({ file, source, addedText: source });
    } catch {
      // Unreadable (binary, races) — skip; missing one file only narrows scope.
    }
  }
  return added;
}

export function untranslatedForLocale(
  source,
  locale,
  scope,
  replacementKeys = new Set(),
  previousLocale = null,
  restagedKeys = new Set(),
) {
  const entries = [];
  for (const [key, value] of source.entries()) {
    if (scope && !scope.has(key)) continue;
    const blank = locale.has(key) && locale.get(key).trim() === "";
    const replaceExisting = replacementKeys.has(key) || blank;
    // A restaged key is an explicit statement that the shipped value is stale,
    // so it skips the idempotence check that treats a value changed on this
    // branch as already done — which is exactly the state it is recovering from.
    if (replaceExisting && !blank && !restagedKeys.has(key) && previousLocale && locale.has(key)) {
      const previousValue = previousLocale.get(key);
      if (locale.get(key) !== previousValue) continue;
    }
    if (!replaceExisting && locale.has(key)) continue;
    entries.push({
      key,
      source: value,
      ...(replaceExisting && locale.has(key) && { replaceExisting: true }),
    });
  }
  return entries;
}

export function exactSourcePeers(source, pendingKeys) {
  const pending = new Set(pendingKeys);
  const keysByValue = new Map();
  for (const [key, value] of source.entries()) {
    if (!keysByValue.has(value)) keysByValue.set(value, []);
    keysByValue.get(value).push(key);
  }
  const peers = new Map();
  for (const key of pending) {
    const value = source.get(key);
    peers.set(
      key,
      value === undefined
        ? []
        : (keysByValue.get(value) ?? []).filter(
            (candidate) => candidate !== key && !pending.has(candidate),
          ),
    );
  }
  return peers;
}

const PLURAL_CATEGORIES = new Set(["zero", "two", "few", "many"]);

// Every dotted node in the catalog: each key plus all of its prefixes. Naming a
// category is not enough to make a key a plural form, so the audit has to see
// the shape of the tree around it.
export function catalogNodes(keys) {
  const nodes = new Set();
  for (const key of keys) {
    const parts = key.split(".");
    for (let index = 1; index <= parts.length; index++) {
      nodes.add(parts.slice(0, index).join("."));
    }
  }
  return nodes;
}

// A node is a plural only when it carries a complete `one`/`other` pair, which
// is the minimum Counterpart needs to pluralize. Presence of a category name
// alone proves nothing: a catalog can hold
// an `other` ("Other", a UI label) among ordinary siblings, and
// treating those as plurals hands the reader a parent's usage evidence for an
// unrelated key. A non-category sibling does not disqualify the node, since a
// real plural can carry extra sub-keys (`…trash.confirm_message` is
// `{dialog, one, other}`).
function isPluralNode(node, nodes) {
  return nodes.has(`${node}.one`) && nodes.has(`${node}.other`);
}

// The key a plural sub-key is actually called by, or null for an ordinary key.
//
// Only Counterpart (`sprintf`) splits a plural across sub-keys: the call site
// names this key and passes a count, so the leaves can never appear literally in
// source and would audit as unreferenced every time. ICU, Polyglot, and
// MessageFormat all hold their plural forms inside the message string, so there
// a leaf named after a category is an ordinary key and must keep its own usage
// evidence.
//
// The walk is iterative because plurals nest: a message taking two counts is
// `<key>.<category>.<category>`, whose intermediate node is never a call site.
export function pluralCallSite(key, syntax, nodes) {
  if (syntax !== "sprintf") return null;
  let current = key;
  let callSite = null;
  while (true) {
    const cut = current.lastIndexOf(".");
    if (cut <= 0) return callSite;
    const parent = current.slice(0, cut);
    if (!isPluralCategory(current.slice(cut + 1)) || !isPluralNode(parent, nodes)) {
      return callSite;
    }
    callSite = parent;
    current = parent;
  }
}

function isPluralCategory(segment) {
  return segment === "one" || segment === "other" || PLURAL_CATEGORIES.has(segment);
}

function branchUsedKeys(context, source, additions) {
  const addedText = additions.map((addition) => addition.addedText).join("\n");
  const used = newlyUsedKeys(source, addedText);
  if (context.syntax === "sprintf") {
    const nodes = catalogNodes(source.keys());
    for (const key of source.keys()) {
      const callSite = pluralCallSite(key, context.syntax, nodes);
      if (
        callSite &&
        (keyOccursAsToken(addedText, callSite) || keyOccursAsDynamicPrefix(addedText, callSite))
      ) {
        used.add(key);
      }
    }
  }
  if (context.messageIndirection) {
    for (const key of newlyUsedMessageKeys(context, source, additions)) used.add(key);
  }
  return used;
}

function auditPendingSource(context, source, entries) {
  const keys = [...new Set(entries.map((entry) => entry.key))];
  if (keys.length === 0) {
    return {
      entries: [],
      totals: { rendered: 0, definitionOnly: 0, testOnly: 0, unreferenced: 0 },
    };
  }
  // Plural call sites ride along in the one grep so a leaf can inherit its
  // caller's status without a second usage pass. The shape check reads the whole
  // catalog, not just the pending keys, since the sibling proving a node plural
  // is usually already translated.
  const nodes = catalogNodes(source.keys());
  const callSites = new Map(keys.map((key) => [key, pluralCallSite(key, context.syntax, nodes)]));
  const extra = [...new Set(callSites.values())].filter(
    (callSite) => callSite !== null && !keys.includes(callSite),
  );
  const usageReport = findUsages({
    path: context.root,
    catalog: context.id,
    context: 0,
    keys: [...keys, ...extra],
    // A plural is routinely called with its form chosen at runtime
    // (`` `<key>.${variant}` ``), which the plain token match rejects because a
    // trailing dot normally means a longer key. For these keys the longer key is
    // one of their own forms.
    dynamicPrefixKeys: new Set(callSites.values().filter(Boolean)),
  });
  if (usageReport.fatal) {
    throw new LocalizationInputError(`source audit: ${usageReport.fatal}`);
  }
  const peers = exactSourcePeers(source, keys);
  const classifiedByKey = new Map(
    usageReport.results.map((result) => [result.key, classifyUsageResult(result)]),
  );
  const auditEntries = usageReport.results
    .filter((result) => keys.includes(result.key))
    .map((result) => {
      let classified = classifiedByKey.get(result.key);
      const callSite = callSites.get(result.key);
      const callSiteClassified = callSite ? classifiedByKey.get(callSite) : undefined;
      // Only a leaf with no literal hits of its own defers to its call site, so a
      // key that genuinely ends in a category word keeps its own evidence.
      const isInheritsCallSite =
        classified.status === "unreferenced" &&
        callSiteClassified !== undefined &&
        callSiteClassified.status !== "unreferenced";
      if (isInheritsCallSite) classified = callSiteClassified;
      return {
        key: result.key,
        status: classified.status,
        ...(callSite && { pluralOf: callSite }),
        ...(classified.usage && {
          usage: { file: classified.usage.file, line: classified.usage.line },
        }),
        exactSourcePeers: peers.get(result.key) ?? [],
      };
    });
  const totals = { rendered: 0, definitionOnly: 0, testOnly: 0, unreferenced: 0 };
  const totalKey = {
    rendered: "rendered",
    "definition-only": "definitionOnly",
    "test-only": "testOnly",
    unreferenced: "unreferenced",
  };
  for (const entry of auditEntries) totals[totalKey[entry.status]] += 1;
  return { entries: auditEntries, totals };
}

// Collapse the flat (locale, key) entries into one row per source key — the view
// SKILL.md says authoring must start from. The English source text is written once per
// key instead of once per (locale, key) pair, and with `targetLocales` given, a group
// missing in the whole target set omits its own `locales` array (the report states the
// set once) — brand-new keys absent everywhere, the normal branch case, would otherwise
// repeat the identical locale list per key, re-growing the report by keys × locales in
// the locale dimension. A group keeps `locales` only when it deviates (already
// translated somewhere). `effectiveFrom` (properties fallback) is kept per locale.
export function groupUntranslatedByKey(entries, targetLocales = null) {
  const byKey = new Map();
  for (const entry of entries) {
    const mapKey = `${entry.catalog}\0${entry.key}`;
    let group = byKey.get(mapKey);
    if (!group) {
      group = {
        catalog: entry.catalog,
        key: entry.key,
        source: entry.source,
        locales: [],
        replaceExistingLocales: [],
      };
      byKey.set(mapKey, group);
    }
    group.locales.push(entry.locale);
    if (entry.replaceExisting) group.replaceExistingLocales.push(entry.locale);
    if (entry.effectiveFrom) {
      (group.effectiveFrom ??= {})[entry.locale] = entry.effectiveFrom;
    }
  }
  return byKey
    .values()
    .map((group) => {
      // Each (key, locale) pair occurs at most once, so covering the full target
      // count means covering the full target set.
      const { locales, replaceExistingLocales, ...rest } = group;
      let operation = {};
      if (replaceExistingLocales.length === locales.length) {
        operation = { replaceExisting: true };
      } else if (replaceExistingLocales.length > 0) {
        operation = { replaceExistingLocales };
      }
      const localeScope =
        targetLocales && locales.length === targetLocales.length ? {} : { locales };
      return { ...rest, ...localeScope, ...operation };
    })
    .toArray();
}

// Walks every source key for one properties-format locale, classifying each as
// already-translated, replace-existing, inherited, or newly missing. Kept as
// its own function (not inlined in the outer `for (const { file, locale } of
// targets)` loop in `run()`) so its `continue`s aren't nested inside that
// outer loop.
function propertiesEntriesForLocale({
  context,
  locale,
  source,
  scope,
  replacementKeys,
  previousLocale,
  restagedKeys = new Set(),
  direct,
  effective,
}) {
  const entries = [];
  let inherited = 0;
  for (const [key, sourceValue] of source.entries()) {
    if (scope && !scope.has(key)) continue;
    if (direct.has(key) && direct.get(key).trim() === "") {
      entries.push({
        catalog: context.id,
        locale,
        key,
        source: sourceValue,
        replaceExisting: true,
      });
      continue;
    }
    if (replacementKeys.has(key)) {
      if (
        !restagedKeys.has(key) &&
        previousLocale &&
        direct.has(key) &&
        direct.get(key) !== previousLocale.get(key)
      ) {
        continue;
      }
      if (!direct.has(key)) {
        const status = classifyAbsence(context, locale, key, effective);
        if (status.inherited) {
          inherited += 1;
          continue;
        }
        entries.push({
          catalog: context.id,
          locale,
          key,
          source: sourceValue,
          effectiveFrom: status.effectiveFrom,
        });
        continue;
      }
      entries.push({
        catalog: context.id,
        locale,
        key,
        source: sourceValue,
        replaceExisting: true,
      });
      continue;
    }
    if (direct.has(key)) continue;
    const status = classifyAbsence(context, locale, key, effective);
    if (status.inherited) {
      inherited += 1;
      continue;
    }
    entries.push({
      catalog: context.id,
      locale,
      key,
      source: sourceValue,
      effectiveFrom: status.effectiveFrom,
    });
  }
  return { entries, inherited };
}

export function run(options) {
  const repo = detectRepo(options.path || process.cwd());
  if (repo.repo === "unknown") {
    throw new LocalizationInputError(`not a known i18n repo: ${repo.reason}`);
  }
  const context = resolveCatalog(repo, options.catalog);
  if (context.error) {
    throw new LocalizationInputError(context.error);
  }

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

  let targets = files
    .filter((file) => file !== sourceFile_)
    .map((file) => ({ file, locale: localeForFile(context, file) }));
  if (options.locale) {
    targets = targets.filter(
      (target) => target.locale === options.locale || target.file === options.locale,
    );
    if (targets.length === 0) {
      throw new LocalizationInputError(`locale ${options.locale} not found`);
    }
  }

  let bundles;
  let bundleErrors;
  let source;
  if (context.format === "properties") {
    ({ bundles, errors: bundleErrors } = loadCatalogBundles(context, dir, files));
    const sourceError = bundleErrors.get(context.source.locale);
    if (sourceError) {
      throw new LocalizationInputError(
        `cannot parse ${sourceError.file}: ${sourceError.error.message}`,
      );
    }
    source = bundles.get(context.source.locale).parsed.values;
  } else {
    source = loadFlat(context, dir, sourceFile_);
  }
  let branchScope = null;
  if (options.branch) {
    try {
      branchScope = resolveBranchForkPoint(context.root);
    } catch (error) {
      throw new LocalizationInputError(`${error.message}; pass --since <ref>`);
    }
  }
  const since = options.since ?? branchScope?.forkPoint ?? null;
  let scope = null;
  const replacementKeys = new Set();
  if (since) {
    const previousSource = sourceAtReference(context, since);
    scope = changedSourceKeys(source, previousSource);
    for (const key of scope) {
      if (
        source.has(key) &&
        previousSource.has(key) &&
        source.get(key) !== previousSource.get(key)
      ) {
        replacementKeys.add(key);
      }
    }
    const outputFile = outputPathInRepo(context, options.out);
    const newlyUsed = branchUsedKeys(
      context,
      source,
      addedSourcesSince(context, since, [outputFile]),
    );
    for (const key of newlyUsed) {
      scope.add(key);
    }
  }
  // A key added on this branch is absent from the fork-point source, so a later
  // rewording of its English cannot be detected by comparison: discovery sees a
  // key that already has locale values and reports nothing to do. Restaging is
  // the explicit statement that those values translate superseded English, and
  // forces the key back into scope as a replacement.
  const restagedKeys = [...new Set(options.restageKeys)];
  for (const key of restagedKeys) {
    if (!source.has(key)) {
      throw new LocalizationInputError(`--restage-key ${key} is not in the current source`);
    }
    if (scope && !scope.has(key)) {
      throw new LocalizationInputError(
        `--restage-key ${key} is not in the selected discovery scope`,
      );
    }
    replacementKeys.add(key);
  }
  const excludedKeys = [...new Set(options.excludeKeys)];
  if (excludedKeys.length > 0) {
    for (const key of excludedKeys) {
      if (!source.has(key)) {
        throw new LocalizationInputError(`--exclude-key ${key} is not in the current source`);
      }
      if (scope && !scope.has(key)) {
        throw new LocalizationInputError(
          `--exclude-key ${key} is not in the selected discovery scope`,
        );
      }
    }
    if (!scope) scope = new Set(source.keys());
    for (const key of excludedKeys) {
      scope.delete(key);
      replacementKeys.delete(key);
    }
  }
  const restaged = new Set(restagedKeys);
  const entries = [];
  let inherited = 0;
  for (const { file, locale } of targets) {
    const previousLocale =
      since && replacementKeys.size > 0 ? catalogAtReference(context, since, file) : null;
    if (context.format !== "properties") {
      const localeFlat = loadFlat(context, dir, file);
      entries.push(
        ...untranslatedForLocale(
          source,
          localeFlat,
          scope,
          replacementKeys,
          previousLocale,
          restaged,
        ).map((entry) => ({
          catalog: context.id,
          locale,
          ...entry,
        })),
      );
      continue;
    }

    const targetError = bundleErrors.get(locale);
    if (targetError) {
      throw new LocalizationInputError(
        `cannot parse ${targetError.file}: ${targetError.error.message}`,
      );
    }
    const direct = bundles.get(locale).parsed.values;
    const effective = effectiveCatalog(context, locale, bundles);
    const collected = propertiesEntriesForLocale({
      context,
      locale,
      source,
      scope,
      replacementKeys,
      previousLocale,
      restagedKeys: restaged,
      direct,
      effective,
    });
    entries.push(...collected.entries);
    inherited += collected.inherited;
  }

  const report = {
    repo: context.repo,
    catalog: context.id,
    sourceFile: sourceFile_,
    since,
    ...(branchScope && {
      forkPointRef: branchScope.remoteRef,
      ...(branchScope.warning && { forkPointWarning: branchScope.warning }),
    }),
    inherited,
    ...(excludedKeys.length > 0 && { excludedKeys }),
    ...(restagedKeys.length > 0 && { restagedKeys }),
    totals: {
      needsTranslation: entries.length,
      inherited,
      explicitAbsences: entries.length + inherited,
    },
    ...(options.auditSource && {
      sourceAudit: auditPendingSource(context, source, entries),
    }),
  };
  // --group-by-key emits one row per source key instead of one per (locale, key), so a
  // many-locale batch is sized to the unique source strings — the collapsed view SKILL.md
  // says authoring must start from — without the agent hand-deduping an oversized report.
  // The full target set is stated once at report level; groups carry `locales` only when
  // they deviate from it.
  if (options.groupByKey) {
    report.locales = targets.map((target) => target.locale);
    // A run resolves exactly one catalog, so every group's catalog equals the
    // report-level one — state it once instead of repeating it per row.
    report.groups = groupUntranslatedByKey(entries, report.locales).map((group) => {
      const trimmed = { ...group };
      delete trimmed.catalog;
      return trimmed;
    });
  } else {
    report.entries = entries;
  }
  return report;
}

export function formatHuman(report) {
  let scope = " across the full source";
  if (report.forkPointRef) {
    scope = ` changed since fork point ${report.forkPointRef} @ ${report.since.slice(0, 12)}`;
  } else if (report.since) {
    scope = ` changed since ${report.since}`;
  }
  if (report.groups) {
    const fullSet = report.locales ?? [];
    const missing = report.groups.reduce(
      (sum, group) => sum + (group.locales ?? fullSet).length,
      0,
    );
    const lines = [
      `untranslated: ${report.groups.length} source key(s), ${missing} missing translation(s)${scope}; ${report.inherited} inherited value(s)`,
      ...(report.locales
        ? [`  target locales (${report.locales.length}): ${report.locales.join(", ")}`]
        : []),
      ...(report.forkPointWarning ? [`  fork-point warning: ${report.forkPointWarning}`] : []),
      ...(report.excludedKeys?.length > 0
        ? [
            `  excluded by user choice (${report.excludedKeys.length}): ${report.excludedKeys.join(", ")}`,
          ]
        : []),
      ...(report.restagedKeys?.length > 0
        ? [
            `  restaged by user choice (${report.restagedKeys.length}): ${report.restagedKeys.join(", ")}`,
          ]
        : []),
    ];
    if (report.sourceAudit) {
      const totals = report.sourceAudit.totals;
      lines.push(
        `  source audit: ${totals.rendered} rendered, ${totals.definitionOnly} definition-only, ${totals.testOnly} test-only, ${totals.unreferenced} unreferenced`,
      );
      for (const entry of report.sourceAudit.entries) {
        if (entry.status === "rendered" && entry.exactSourcePeers.length === 0) continue;
        const usage = entry.usage ? ` @ ${entry.usage.file}:${entry.usage.line}` : "";
        const plural = entry.pluralOf ? ` (plural form of ${entry.pluralOf})` : "";
        const peers =
          entry.exactSourcePeers.length > 0
            ? `; exact source peer(s): ${entry.exactSourcePeers.slice(0, 3).join(", ")}${entry.exactSourcePeers.length > 3 ? ` (+${entry.exactSourcePeers.length - 3})` : ""}`
            : "";
        lines.push(`    [${entry.status}] ${entry.key}${plural}${usage}${peers}`);
      }
    }
    // Keys sharing a namespace print under one prefix header, so the dominant
    // cost of a component batch — the repeated key prefix — is paid once.
    let currentPrefix = null;
    for (const group of report.groups) {
      const cut = group.key.lastIndexOf(".");
      const prefix = cut > 0 ? group.key.slice(0, cut) : null;
      const leaf = cut > 0 ? group.key.slice(cut + 1) : group.key;
      if (prefix !== currentPrefix) {
        currentPrefix = prefix;
        if (prefix) lines.push(`  ${prefix}.*`);
      }
      const deviation = group.locales ? ` → [${group.locales.join(", ")}]` : "";
      let operation = "";
      if (group.replaceExisting) {
        operation = " [replace existing]";
      } else if (group.replaceExistingLocales) {
        operation = ` [replace existing: ${group.replaceExistingLocales.join(", ")}]`;
      }
      lines.push(
        `${prefix ? " ".repeat(4) : "  "}${leaf}: ${JSON.stringify(group.source)}${operation}${deviation}`,
      );
    }
    return lines.join("\n");
  }
  const lines = [
    `untranslated: ${report.entries.length} missing translation(s)${scope}; ${report.inherited} inherited value(s)`,
    ...(report.forkPointWarning ? [`  fork-point warning: ${report.forkPointWarning}`] : []),
    ...(report.excludedKeys?.length > 0
      ? [
          `  excluded by user choice (${report.excludedKeys.length}): ${report.excludedKeys.join(", ")}`,
        ]
      : []),
    ...(report.restagedKeys?.length > 0
      ? [
          `  restaged by user choice (${report.restagedKeys.length}): ${report.restagedKeys.join(", ")}`,
        ]
      : []),
  ];
  if (report.sourceAudit) {
    const totals = report.sourceAudit.totals;
    lines.push(
      `  source audit: ${totals.rendered} rendered, ${totals.definitionOnly} definition-only, ${totals.testOnly} test-only, ${totals.unreferenced} unreferenced`,
    );
  }
  for (const entry of report.entries) {
    const effective = entry.effectiveFrom ? ` (effective from ${entry.effectiveFrom})` : "";
    const operation = entry.replaceExisting ? " [replace existing]" : "";
    lines.push(
      `  [${entry.locale}] ${entry.key}: ${JSON.stringify(entry.source)}${operation}${effective}`,
    );
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
    const report = run(options);
    if (options.out) {
      fs.writeFileSync(options.out, `${JSON.stringify(report)}\n`);
      console.log(formatHuman(report));
    } else {
      console.log(options.json ? JSON.stringify(report) : formatHuman(report));
    }
  } catch (error) {
    console.error(`untranslated: ${error.message}`);
    process.exit(error.exitCode ?? 1);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}
