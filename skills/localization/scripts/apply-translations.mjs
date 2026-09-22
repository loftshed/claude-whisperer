#!/usr/bin/env node
// Apply an approved batch of translations. This is the skill's only writer:
// it validates every entry before writing, refuses orphans and overwrites, and
// preserves JSON formatting and existing key order, untouched TypeScript text,
// and properties bytes. Existing keys are never moved; a new JSON key is
// inserted at its natural-sort position among its neighbors, so order-enforcing
// repos stay lint-clean. Every object written into has its key order reported,
// since no gate in this skill checks it.
//
//   node apply-translations.mjs [--path <repo>] [--catalog <id>] [--scope <scope.json>] [--dry-run] [--verbose] [--force] [--approved-digest <sha256>] [payload.json]
//
// If payload.json is omitted or is "-", JSON is read from stdin. Flat arrays
// and compact batch manifests are accepted. A non-dry-run CLI write requires
// the digest emitted by preflight-batch.mjs.

import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  detectRepo,
  fileForLocale,
  localeFiles,
  resolveCatalog,
  sourceFile,
  translationsDir,
} from "./lib/adapters.mjs";
import {
  approvalDigest,
  batchDigest,
  expandBatch,
  readBatch,
  readScope,
  validateBatchSignatures,
  validateCoverage,
  validateScopeSources,
} from "./lib/batch.mjs";
import { parseJsonCatalog } from "./lib/json-catalog.mjs";
import { appendProperty, parseProperties, replaceProperty } from "./lib/properties.mjs";
import { parseTypeScriptCatalog, setTypeScriptCatalogValue } from "./lib/typescript-catalog.mjs";

export class ApplyError extends Error {
  constructor(message, exitCode) {
    super(message);
    this.name = "ApplyError";
    this.exitCode = exitCode;
  }
}

function inputError(message) {
  return new ApplyError(message, 2);
}

function refusal(message) {
  return new ApplyError(message, 1);
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function takeValue(rest, flag) {
  const value = rest.shift();
  if (!value || value.startsWith("--")) throw inputError(`${flag} requires a value`);
  return value;
}

// Apply one parsed argument to `options`, consuming its value from `rest` when
// the flag takes one. Kept as its own function (rather than a switch inline in
// the while loop below) so each case's `break` ends the switch, not the loop.
function applyArg(options, rest, argument) {
  switch (argument) {
    case "--path": {
      options.path = takeValue(rest, argument);
      break;
    }
    case "--catalog": {
      options.catalog = takeValue(rest, argument);
      break;
    }
    case "--scope": {
      options.scopePath = takeValue(rest, argument);
      break;
    }
    case "--dry-run": {
      options.dryRun = true;
      break;
    }
    case "--force": {
      options.force = true;
      break;
    }
    case "--approved-digest": {
      options.approvedDigest = takeValue(rest, argument);
      if (!/^[a-f0-9]{64}$/i.test(options.approvedDigest)) {
        throw inputError("--approved-digest must be a 64-character SHA-256 digest");
      }
      options.approvedDigest = options.approvedDigest.toLowerCase();
      break;
    }
    case "--verbose": {
      options.verbose = true;
      break;
    }
    default: {
      if (argument.startsWith("--")) throw inputError(`unknown argument ${argument}`);
      if (options.payloadPath !== null) throw inputError("provide at most one payload path");
      options.payloadPath = argument;
    }
  }
}

export function parseArgs(argv) {
  const options = {
    path: null,
    catalog: null,
    dryRun: false,
    force: false,
    approvedDigest: null,
    scopePath: null,
    payloadPath: null,
    verbose: false,
  };
  const rest = [...argv];
  while (rest.length > 0) {
    const argument = rest.shift();
    applyArg(options, rest, argument);
  }
  return options;
}

function setOwn(object, key, value) {
  Object.defineProperty(object, key, {
    configurable: true,
    enumerable: true,
    value,
    writable: true,
  });
}

export function readPayload(payloadPath) {
  return readBatch(payloadPath);
}

function propertyAt(object, key, keyStyle) {
  if (keyStyle === "flat") {
    return Object.hasOwn(object, key)
      ? { exists: true, value: object[key] }
      : { exists: false, value: undefined };
  }

  const parts = key.split(".");
  let current = object;
  for (const part of parts) {
    if (!isObject(current) || !Object.hasOwn(current, part)) {
      return { exists: false, value: undefined };
    }
    current = current[part];
  }
  return { exists: true, value: current };
}

// Character weight used by the `natural-compare` package, which is what ESLint's
// `jsonc/sort-keys` (`natural: true, caseSensitive: true`) orders keys with. The
// remapping is not raw code-unit order: `_` (0x5F) weighs 58, below digits and
// letters, where a raw comparison would put it between `Z` and `a`. Catalogs
// with `snake_case` and `camelCase` siblings therefore sort in the opposite
// direction under a raw comparison, so this must stay a faithful port.
function characterWeight(text, position) {
  // eslint-disable-next-line unicorn/prefer-code-point -- parity with the package, which reads code units.
  const code = text.charCodeAt(position) || 0;
  if (code < 45 || code > 127) return code;
  if (code < 46) return 65; // -
  if (code < 48) return code - 1;
  if (code < 58) return code + 18; // 0-9
  if (code < 65) return code - 11;
  if (code < 91) return code + 11; // A-Z
  if (code < 97) return code - 37; // includes _ → 58
  if (code < 123) return code + 5; // a-z
  return code - 63;
}

// Read the digit run whose first character sits at `position - 1`, returning its
// numeric value and the index one past the run. Weights 66–75 are the digits.
function digitRunAt(text, position) {
  let end = position;
  for (;;) {
    const weight = characterWeight(text, end);
    if (weight > 65 && weight < 76) end++;
    else break;
  }
  return { value: Number(text.slice(position - 1, end)), end };
}

// Case-sensitive natural comparison: digit runs compare numerically, everything
// else by the weights above — the same ordering as ESLint's `jsonc/sort-keys`
// with `natural: true, caseSensitive: true`, which consumer repos use on
// catalogs.
export function naturalCompare(left, right) {
  if (left === right) return 0;
  let leftPosition = 0;
  let rightPosition = 0;
  for (;;) {
    let leftWeight = characterWeight(left, leftPosition++);
    let rightWeight = characterWeight(right, rightPosition++);
    // Both sides opened a digit run (a leading `0` weighs 66 and is excluded, so
    // it compares character by character, as the package does).
    if (leftWeight > 66 && leftWeight < 76 && rightWeight > 66 && rightWeight < 76) {
      const leftRun = digitRunAt(left, leftPosition);
      const rightRun = digitRunAt(right, rightPosition);
      leftWeight = leftRun.value;
      rightWeight = rightRun.value;
      leftPosition = leftRun.end;
      rightPosition = rightRun.end;
    }
    if (leftWeight !== rightWeight) return leftWeight < rightWeight ? -1 : 1;
    if (rightWeight === 0) return 0;
  }
}

export function isNaturallySorted(keys) {
  for (let index = 1; index < keys.length; index++) {
    if (naturalCompare(keys[index - 1], keys[index]) > 0) return false;
  }
  return true;
}

// The position where `key` belongs among its immediate neighbors: the first
// index whose predecessor sorts before the key and whose successor sorts after
// it. Global sortedness is deliberately not required — one legacy out-of-order
// pair elsewhere in a large catalog object must not disable sorted insertion for
// every future key in it. Both new adjacencies are ordered by construction, so
// the write can never introduce an order violation of its own. A position always
// exists for a key the object does not already hold.
export function sortedInsertIndex(keys, key) {
  for (let index = 0; index <= keys.length; index++) {
    const isAfterPredecessor = index === 0 || naturalCompare(keys[index - 1], key) < 0;
    const isBeforeSuccessor = index === keys.length || naturalCompare(key, keys[index]) < 0;
    if (isAfterPredecessor && isBeforeSuccessor) return index;
  }
  return keys.length;
}

// Add a new key to `object` at its sorted position. Existing keys never move
// relative to each other — insertion only rebuilds the tail after the insertion
// point with identical relative order.
function insertProperty(object, key, value) {
  const keys = Object.keys(object);
  const index = sortedInsertIndex(keys, key);
  if (index < keys.length) {
    const tail = keys.slice(index).map((tailKey) => [tailKey, object[tailKey]]);
    for (const [tailKey] of tail) delete object[tailKey];
    setOwn(object, key, value);
    for (const [tailKey, tailValue] of tail) setOwn(object, tailKey, tailValue);
    return;
  }
  setOwn(object, key, value);
}

// Writes `key` and returns the dotted path of the object that now holds it ("" is
// the file root), so the caller can report that object's key order.
function setProperty(object, key, value, keyStyle) {
  if (keyStyle === "flat") {
    if (Object.hasOwn(object, key)) setOwn(object, key, value);
    else insertProperty(object, key, value);
    return "";
  }

  const parts = key.split(".");
  const leaf = parts.pop();
  let current = object;
  for (const part of parts) {
    if (!Object.hasOwn(current, part)) insertProperty(current, part, {});
    if (!isObject(current[part])) {
      throw refusal(`cannot create ${key}: ${part} is not an object`);
    }
    current = current[part];
  }
  if (Object.hasOwn(current, leaf)) setOwn(current, leaf, value);
  else insertProperty(current, leaf, value);
  return parts.join(".");
}

// Paths among `paths` whose object is not fully key-sorted. The writer's own
// insertions are always ordered, so anything reported here is a pre-existing
// break — but it still means the file fails a whole-object order lint, which no
// gate in this skill checks.
function unsortedObjectPaths(root, paths) {
  const unsorted = [];
  const ordered = [...paths].sort(naturalCompare);
  for (const objectPath of ordered) {
    const parts = objectPath === "" ? [] : objectPath.split(".");
    let current = root;
    for (const part of parts) current = current?.[part];
    if (isObject(current) && !isNaturallySorted(Object.keys(current))) unsorted.push(objectPath);
  }
  return unsorted;
}

export function detectFormatting(raw) {
  const indentMatch = raw.match(/(?:\r?\n)([\t ]+)"/);
  return {
    indent: indentMatch?.[1] ?? "  ",
    newline: raw.includes("\r\n") ? "\r\n" : "\n",
    trailingNewline: /\r?\n$/.test(raw),
  };
}

function serialize(object, formatting) {
  let output = JSON.stringify(object, null, formatting.indent);
  if (formatting.newline === "\r\n") output = output.replaceAll("\n", "\r\n");
  if (formatting.trailingNewline) output += formatting.newline;
  return output;
}

function loadLocale(file) {
  const { buffer, parsed } = parseWithDupCheck(file, "target", parseJsonCatalog);
  const raw = buffer.toString("utf8");
  return { buffer, object: parsed.object, formatting: detectFormatting(raw) };
}

// Preflight and stage one catalog's entries in memory. Returns the staged file
// writes and per-locale results; nothing touches disk here.
function stageJsonCatalog(context, entries, force) {
  const dir = translationsDir(context);
  let files;
  try {
    files = localeFiles(context);
  } catch (error) {
    throw inputError(`cannot read ${dir}: ${error.message}`);
  }
  const sourceName = sourceFile(context);
  if (!files.includes(sourceName)) {
    throw inputError(`source file ${sourceName} not found in ${dir}`);
  }

  const source = parseWithDupCheck(path.join(dir, sourceName), "source", parseJsonCatalog)
    .parsed.values;
  const targets = new Map();
  const written = new Map();
  const touchedObjects = new Map();

  for (const entry of entries) {
    const file = fileForLocale(context, entry.locale);
    if (file === sourceName || !files.includes(file)) {
      throw inputError(`target locale ${entry.locale} not found`);
    }
    if (!source.has(entry.key)) {
      throw refusal(`refusing orphan key ${entry.key}: absent from ${sourceName}`);
    }

    if (!targets.has(file)) {
      targets.set(file, loadLocale(path.join(dir, file)));
      written.set(entry.locale, []);
      touchedObjects.set(entry.locale, new Set());
    }
    const target = targets.get(file);
    const existing = propertyAt(target.object, entry.key, context.keyStyle);
    if (!force && existing.exists && existing.value !== "" && !entry.replaceExisting) {
      throw refusal(`refusing to overwrite ${entry.locale}:${entry.key}`);
    }
    const objectPath = setProperty(target.object, entry.key, entry.value, context.keyStyle);
    touchedObjects.get(entry.locale).add(objectPath);
    written.get(entry.locale).push(entry.key);
  }

  const unsorted = new Map(
    [...touchedObjects].map(([locale, paths]) => [
      locale,
      unsortedObjectPaths(targets.get(`${locale}.json`).object, paths),
    ]),
  );
  const staged = [...targets].map(([file, target]) => ({
    file: path.join(dir, file),
    original: target.buffer,
    content: serialize(target.object, target.formatting),
  }));
  return { staged, localeResults: localeResultsFor(context, written, unsorted) };
}

function localeResultsFor(context, written, unsorted) {
  return written
    .entries()
    .map(([locale, keys]) => ({
      catalog: context.id,
      locale,
      count: keys.length,
      keys,
      unsortedObjects: unsorted?.get(locale) ?? [],
    }))
    .toArray();
}

// Read and parse a catalog file, rejecting anything with duplicate keys before
// it can be staged. `parse` adapts the format-specific parser to a buffer.
function parseWithDupCheck(file, role, parse) {
  let buffer;
  let parsed;
  try {
    buffer = fs.readFileSync(file);
    parsed = parse(buffer);
  } catch (error) {
    throw refusal(`cannot read ${role} ${file}: ${error.message}`);
  }
  if (parsed.duplicates.length > 0) {
    const duplicate = parsed.duplicates[0];
    throw refusal(
      `duplicate key ${duplicate.key} in ${file} at lines ${duplicate.startLine}-${duplicate.endLine}`,
    );
  }
  return { buffer, parsed };
}

function stagePropertiesCatalog(context, entries, force) {
  const dir = translationsDir(context);
  let files;
  try {
    files = localeFiles(context);
  } catch (error) {
    throw inputError(`cannot read ${dir}: ${error.message}`);
  }

  const sourceFile_ = sourceFile(context);
  if (!files.includes(sourceFile_)) {
    throw inputError(`source file ${sourceFile_} not found in ${dir}`);
  }
  const source = parseWithDupCheck(path.join(dir, sourceFile_), "source", parseProperties).parsed
    .values;
  const targets = new Map();
  const written = new Map();

  for (const entry of entries) {
    const file = fileForLocale(context, entry.locale);
    if (file === sourceFile_ || !files.includes(file)) {
      throw inputError(`target locale ${entry.locale} not found`);
    }
    if (!source.has(entry.key)) {
      throw refusal(`refusing orphan key ${entry.key}: absent from ${sourceFile_}`);
    }

    if (!targets.has(file)) {
      const target = parseWithDupCheck(path.join(dir, file), "target", parseProperties);
      targets.set(file, { ...target, replacements: [], appends: [] });
      written.set(entry.locale, []);
    }
    const target = targets.get(file);
    if (target.parsed.values.has(entry.key)) {
      const existing = target.parsed.values.get(entry.key);
      if (existing !== "" && !force && !entry.replaceExisting) {
        throw refusal(`refusing to overwrite ${entry.locale}:${entry.key}`);
      }
      target.replacements.push({ key: entry.key, value: entry.value });
    } else {
      target.appends.push({ key: entry.key, value: entry.value });
    }
    written.get(entry.locale).push(entry.key);
  }

  const staged = [...targets].map(([file, target]) => {
    let content = target.buffer;
    const replacements = target.replacements.sort(
      (left, right) =>
        target.parsed.locations.get(right.key).startLine -
        target.parsed.locations.get(left.key).startLine,
    );
    for (const replacement of replacements) {
      content = replaceProperty(content, target.parsed, replacement.key, replacement.value);
    }
    for (const addition of target.appends) {
      content = appendProperty(content, target.parsed, addition.key, addition.value);
    }
    return { file: path.join(dir, file), original: target.buffer, content };
  });
  return { staged, localeResults: localeResultsFor(context, written) };
}

function stageTypeScriptCatalog(context, entries, force) {
  const dir = translationsDir(context);
  let files;
  try {
    files = localeFiles(context);
  } catch (error) {
    throw inputError(`cannot read ${dir}: ${error.message}`);
  }

  const sourceFile_ = sourceFile(context);
  if (!files.includes(sourceFile_)) {
    throw inputError(`source file ${sourceFile_} not found in ${dir}`);
  }
  const source = parseWithDupCheck(path.join(dir, sourceFile_), "source", (buffer) =>
    parseTypeScriptCatalog(buffer, context),
  ).parsed;
  const targets = new Map();
  const written = new Map();

  for (const entry of entries) {
    const file = fileForLocale(context, entry.locale);
    if (file === sourceFile_ || !files.includes(file)) {
      throw inputError(`target locale ${entry.locale} not found`);
    }
    if (!source.values.has(entry.key)) {
      throw refusal(`refusing orphan key ${entry.key}: absent from ${sourceFile_}`);
    }

    if (!targets.has(file)) {
      targets.set(file, {
        ...parseWithDupCheck(path.join(dir, file), "target", (buffer) =>
          parseTypeScriptCatalog(buffer, context),
        ),
        entries: [],
      });
      written.set(entry.locale, []);
    }
    const target = targets.get(file);
    if (target.parsed.values.has(entry.key)) {
      const existing = target.parsed.values.get(entry.key);
      if (existing !== "" && !force && !entry.replaceExisting) {
        throw refusal(`refusing to overwrite ${entry.locale}:${entry.key}`);
      }
      if (!target.parsed.locations.get(entry.key).writable) {
        throw refusal(`cannot edit generated or shared value ${entry.locale}:${entry.key}`);
      }
    }
    target.entries.push(entry);
    written.get(entry.locale).push(entry.key);
  }

  const staged = [...targets].map(([file, target]) => {
    let content = target.buffer;
    for (const entry of target.entries) {
      const parsed = parseTypeScriptCatalog(content, context);
      try {
        content = setTypeScriptCatalogValue(content, parsed, entry.key, entry.value, {
          force: force || Boolean(entry.replaceExisting),
          sourceLocation: source.locations.get(entry.key),
        });
      } catch (error) {
        throw refusal(`${entry.locale}:${entry.key}: ${error.message}`);
      }
    }
    return { file: path.join(dir, file), original: target.buffer, content };
  });
  return { staged, localeResults: localeResultsFor(context, written) };
}

export function applyTranslations(options, payload) {
  const expanded = expandBatch(payload, options);
  if (!options.dryRun && options.approvedDigest && !options.scope) {
    throw inputError("approved writes require the discovery scope used by preflight");
  }
  const entries = expanded.entries;
  if (options.scope) {
    const coverage = validateCoverage(entries, options.scope);
    if (!coverage.ok) {
      throw refusal(
        `discovery scope mismatch: ${coverage.missing.length} missing, ${coverage.extra.length} extra, ${coverage.operationMismatches.length} operation mismatch(es)`,
      );
    }
    const sourceIssues = validateScopeSources(options.scope, options);
    if (sourceIssues.length > 0) {
      throw refusal(
        `source changed since preflight: ${sourceIssues.map((issue) => `${issue.catalog}:${issue.key}`).join(", ")}`,
      );
    }
    const structureIssues = validateBatchSignatures(entries, options);
    if (structureIssues.length > 0) {
      throw refusal(
        `batch structure changed since preflight: ${structureIssues.map((issue) => `${issue.locale}:${issue.key} ${issue.code}`).join(", ")}`,
      );
    }
  }
  const repo = detectRepo(options.path || process.cwd());
  if (repo.repo === "unknown") throw inputError(`not a known i18n repo: ${repo.reason}`);

  // Group entries by resolved catalog; the whole batch preflights before any
  // catalog's files are written.
  const groups = new Map();
  const resolvedEntries = new Set();
  for (const entry of entries) {
    const context = resolveCatalog(repo, entry.catalog ?? options.catalog);
    if (context.error) throw inputError(context.error);
    const identity = `${context.id}\0${entry.locale}\0${entry.key}`;
    if (resolvedEntries.has(identity)) {
      throw inputError(`duplicate payload entry for ${entry.locale}:${entry.key}`);
    }
    resolvedEntries.add(identity);
    if (!groups.has(context.id)) groups.set(context.id, { ctx: context, entries: [] });
    groups.get(context.id).entries.push(entry);
  }

  const staged = [];
  const localeResults = [];
  for (const group of groups.values()) {
    let result;
    if (group.ctx.format === "properties") {
      result = stagePropertiesCatalog(group.ctx, group.entries, options.force);
    } else if (group.ctx.format === "typescript") {
      result = stageTypeScriptCatalog(group.ctx, group.entries, options.force);
    } else {
      result = stageJsonCatalog(group.ctx, group.entries, options.force);
    }
    staged.push(...result.staged);
    localeResults.push(...result.localeResults);
  }

  const targets = targetSnapshots(repo, staged);
  const digest = options.scope
    ? approvalDigest(entries, options.scope, targets)
    : batchDigest(entries);
  if (options.approvedDigest && options.approvedDigest.toLowerCase() !== digest) {
    throw refusal(
      `approved digest mismatch: expected ${options.approvedDigest}, batch or target catalogs changed; rerun preflight-batch.mjs`,
    );
  }
  if (!options.dryRun) {
    for (const write of staged) {
      if (!fs.readFileSync(write.file).equals(write.original)) {
        throw refusal(
          `target catalog changed while staging ${write.file}; rerun preflight-batch.mjs`,
        );
      }
    }
    for (const write of staged) {
      fs.writeFileSync(write.file, write.content);
    }
  }

  return {
    repo: repo.repo,
    dryRun: Boolean(options.dryRun),
    digest,
    targets,
    provenance: expanded.provenance,
    localeResults,
  };
}

function targetSnapshots(repo, staged) {
  const sources = new Set();
  const aliases = new Map();
  for (const catalog of repo.catalogs) {
    const files = localeFiles({ ...catalog, root: repo.root });
    for (const file of files) {
      const absolute = path.join(repo.root, catalog.dir, file);
      const resolved = fs.realpathSync(absolute);
      if (file === sourceFile(catalog)) sources.add(resolved);
      if (!aliases.has(resolved)) aliases.set(resolved, new Set());
      aliases.get(resolved).add(absolute);
    }
  }
  return staged.map(({ file, original }) => {
    const resolved = fs.realpathSync(file);
    if (sources.has(resolved)) throw refusal(`target ${file} resolves to a source catalog`);
    if (aliases.get(resolved)?.size > 1) {
      throw refusal(`multiple locales resolve to the same target catalog ${resolved}`);
    }
    return {
      logicalFile: path.relative(repo.root, file),
      file: resolved,
      sha256: createHash("sha256").update(original).digest("hex"),
    };
  });
}

// Key-order state of every object this batch wrote into. Insertions are ordered
// by construction, so a report here is a pre-existing break — but the file still
// fails a whole-object order lint, and no gate in this skill checks key order, so
// the operator has to see it without running the consumer repo's linter.
export function keyOrderLines(localeResults, shouldQualifyCatalog = false) {
  const unsorted = localeResults.filter((result) => result.unsortedObjects?.length > 0);
  if (unsorted.length === 0) return ["  key order: written keys are in sorted position"];
  const lines = [
    `  key order: new keys inserted in sorted position, but ${unsorted.length} file(s) ` +
      "carry pre-existing out-of-order keys — an order lint there still fails",
  ];
  for (const result of unsorted) {
    const locale = shouldQualifyCatalog ? `${result.catalog} ${result.locale}` : result.locale;
    const objects = result.unsortedObjects.map((objectPath) => objectPath || "(root)");
    lines.push(`    ${locale}: ${objects.join(", ")}`);
  }
  return lines;
}

export function formatHuman(report, { verbose = false } = {}) {
  const total = report.localeResults.reduce((sum, result) => sum + result.count, 0);
  const catalogs = new Set(report.localeResults.map((result) => result.catalog));
  const action = report.dryRun ? "would write" : "wrote";
  const lines = [`apply-translations: ${action} ${total} translation(s) in ${report.repo}`];
  const counts = report.localeResults.map((result) => result.count);
  const uniform = counts.length > 0 && counts.every((count) => count === counts[0]);
  lines.push(
    uniform
      ? `  ${report.localeResults.length} locale file(s) × ${counts[0]}`
      : `  ${report.localeResults.length} locale file(s), non-uniform counts`,
  );
  lines.push(...keyOrderLines(report.localeResults, catalogs.size > 1));
  if (verbose) {
    for (const result of report.localeResults) {
      const locale = catalogs.size > 1 ? `${result.catalog} ${result.locale}` : result.locale;
      lines.push(`  ${locale}: ${result.count}`);
      for (const key of result.keys) lines.push(`    ${key}`);
    }
  }
  return lines.join("\n");
}

function main() {
  try {
    const options = parseArgs(process.argv.slice(2));
    if (!options.dryRun && !options.approvedDigest) {
      throw inputError("non-dry-run writes require --approved-digest from preflight-batch.mjs");
    }
    if (!options.dryRun && !options.scopePath) {
      throw inputError("non-dry-run writes require --scope from preflight-batch.mjs");
    }
    if (options.scopePath) options.scope = readScope(options.scopePath);
    const payload = readPayload(options.payloadPath);
    console.log(formatHuman(applyTranslations(options, payload), { verbose: options.verbose }));
  } catch (error) {
    console.error(`apply-translations: ${error.message}`);
    process.exit(error.exitCode ?? 1);
  }
}

export { validatePayload } from "./lib/batch.mjs";

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}
