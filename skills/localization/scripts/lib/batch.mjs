import { createHash } from "node:crypto";
import fs from "node:fs";

import {
  detectRepo,
  localeFiles,
  localeForFile,
  resolveCatalog,
  sourceFile,
  sourceLocale,
  translationsDir,
} from "./adapters.mjs";
import { effectiveCatalog, loadCatalogBundles, translatedRendering } from "./catalog.mjs";
import { diffSignature, signatureFor } from "./placeholders.mjs";

export class BatchInputError extends Error {
  constructor(message) {
    super(message);
    this.name = "BatchInputError";
    this.exitCode = 2;
  }
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.trim() !== "";
}

export function validatePayload(payload) {
  if (!Array.isArray(payload)) throw new BatchInputError("payload must be a JSON array");

  const seen = new Set();
  return payload.map((entry, index) => {
    if (!isObject(entry)) throw new BatchInputError(`payload[${index}] must be an object`);
    if (!isNonEmptyString(entry.locale)) {
      throw new BatchInputError(`payload[${index}].locale must be a non-empty string`);
    }
    if (!isNonEmptyString(entry.key)) {
      throw new BatchInputError(`payload[${index}].key must be a non-empty string`);
    }
    if (!isNonEmptyString(entry.value)) {
      throw new BatchInputError(`payload[${index}].value must be a non-empty string`);
    }
    if (entry.catalog != null && !isNonEmptyString(entry.catalog)) {
      throw new BatchInputError(
        `payload[${index}].catalog must be a non-empty string when present`,
      );
    }
    if (entry.replaceExisting != null && typeof entry.replaceExisting !== "boolean") {
      throw new BatchInputError(`payload[${index}].replaceExisting must be a boolean when present`);
    }

    const locale = entry.locale.replace(/\.json$/, "");
    const key = entry.key;
    const catalog = entry.catalog ?? null;
    const identity = `${catalog ?? ""}\0${locale}\0${key}`;
    if (seen.has(identity)) {
      throw new BatchInputError(`duplicate payload entry for ${locale}:${key}`);
    }
    seen.add(identity);
    return {
      catalog,
      locale,
      key,
      value: entry.value,
      ...(entry.replaceExisting && { replaceExisting: true }),
    };
  });
}

export function readBatch(payloadPath) {
  let raw;
  try {
    raw =
      payloadPath && payloadPath !== "-"
        ? fs.readFileSync(payloadPath, "utf8")
        : fs.readFileSync(0, "utf8");
  } catch (error) {
    throw new BatchInputError(`cannot read payload: ${error.message}`);
  }
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new BatchInputError(`cannot parse payload JSON: ${error.message}`);
  }
}

function detectedRepo(options) {
  const repo = detectRepo(options.path || process.cwd());
  if (repo.repo === "unknown") {
    throw new BatchInputError(`not a known i18n repo: ${repo.reason}`);
  }
  return repo;
}

function resolvedEntries(entries, options) {
  const repo = detectedRepo(options);
  const resolved = entries.map((entry) => {
    const context = resolveCatalog(repo, entry.catalog ?? options.catalog);
    if (context.error) throw new BatchInputError(context.error);
    return { ...entry, catalog: context.id };
  });
  return validatePayload(resolved);
}

function targetLocales(context) {
  const source = sourceFile(context);
  return localeFiles(context)
    .filter((file) => file !== source)
    .map((file) => localeForFile(context, file));
}

function validateLocales(locales, available, label) {
  if (!Array.isArray(locales) || locales.length === 0) {
    throw new BatchInputError(`${label} must be a non-empty locale array`);
  }
  const seen = new Set();
  return locales.map((raw, index) => {
    if (!isNonEmptyString(raw)) {
      throw new BatchInputError(`${label}[${index}] must be a non-empty string`);
    }
    const locale = raw.replace(/\.json$/, "");
    if (seen.has(locale)) throw new BatchInputError(`duplicate locale ${locale} in ${label}`);
    if (!available.has(locale)) {
      throw new BatchInputError(`target locale ${locale} not found`);
    }
    seen.add(locale);
    return locale;
  });
}

function effectiveValues(context, locales, bundles) {
  return new Map(locales.map((locale) => [locale, effectiveCatalog(context, locale, bundles)]));
}

function copiedValue(context, effective, locale, key) {
  const value = translatedRendering(context, locale, key, effective);
  if (value === null) {
    throw new BatchInputError(`cannot copy ${key} for ${locale}: no translated rendering`);
  }
  return value;
}

function normalizeValues(values, label) {
  const normalized = {};
  for (const [rawLocale, value] of Object.entries(values)) {
    const locale = rawLocale.replace(/\.json$/, "");
    if (Object.hasOwn(normalized, locale)) {
      throw new BatchInputError(`${label} contains duplicate locale ${locale}`);
    }
    normalized[locale] = value;
  }
  return normalized;
}

function expandManifest(manifest, options) {
  if (!Array.isArray(manifest.translations) || manifest.translations.length === 0) {
    throw new BatchInputError("batch manifest must contain a non-empty translations array");
  }
  if (manifest.review != null && !Array.isArray(manifest.review)) {
    throw new BatchInputError("batch manifest review must be an array when present");
  }
  const repo = detectedRepo(options);
  const context = resolveCatalog(repo, manifest.catalog ?? options.catalog);
  if (context.error) throw new BatchInputError(context.error);

  let files;
  try {
    files = localeFiles(context);
  } catch (error) {
    throw new BatchInputError(`cannot read ${translationsDir(context)}: ${error.message}`);
  }
  const available = new Set(targetLocales(context));
  const locales = validateLocales(manifest.locales, available, "locales");
  const { bundles, errors } = loadCatalogBundles(context, translationsDir(context), files);
  const sourceError = errors.get(sourceLocale(context));
  if (sourceError) {
    throw new BatchInputError(`cannot read ${sourceFile(context)}: ${sourceError.error.message}`);
  }
  const source = bundles.get(sourceLocale(context)).parsed.values;
  const effective = effectiveValues(context, locales, bundles);
  const seenKeys = new Set();
  const entries = [];
  let copied = 0;
  let authored = 0;

  for (const [index, translation] of manifest.translations.entries()) {
    const label = `translations[${index}]`;
    if (!isObject(translation)) {
      throw new BatchInputError(`${label} must be an object`);
    }
    if (!isNonEmptyString(translation.key)) {
      throw new BatchInputError(`${label}.key must be a non-empty string`);
    }
    if (seenKeys.has(translation.key)) {
      throw new BatchInputError(`duplicate manifest key ${translation.key}`);
    }
    seenKeys.add(translation.key);
    if (!source.has(translation.key)) {
      throw new BatchInputError(
        `refusing orphan key ${translation.key}: absent from ${sourceFile(context)}`,
      );
    }
    const rawValues = translation.values ?? {};
    if (!isObject(rawValues)) throw new BatchInputError(`${label}.values must be an object`);
    const values = normalizeValues(rawValues, `${label}.values`);
    if (translation.copyFrom != null && !isNonEmptyString(translation.copyFrom)) {
      throw new BatchInputError(`${label}.copyFrom must be a non-empty string`);
    }
    if (translation.replaceExisting != null && typeof translation.replaceExisting !== "boolean") {
      throw new BatchInputError(`${label}.replaceExisting must be a boolean when present`);
    }
    if (translation.replaceExisting && translation.replaceExistingLocales != null) {
      throw new BatchInputError(
        `${label} cannot combine replaceExisting with replaceExistingLocales`,
      );
    }
    if (!translation.copyFrom && Object.keys(values).length === 0) {
      throw new BatchInputError(`${label} needs copyFrom or locale values`);
    }
    if (translation.copyFrom) {
      if (!source.has(translation.copyFrom)) {
        throw new BatchInputError(
          `${label}.copyFrom key ${translation.copyFrom} is absent from ${sourceFile(context)}`,
        );
      }
      if (source.get(translation.copyFrom) !== source.get(translation.key)) {
        throw new BatchInputError(
          `${label}.copyFrom must have the same English source as ${translation.key}`,
        );
      }
    }
    const rowLocales = translation.locales
      ? validateLocales(translation.locales, available, `${label}.locales`)
      : locales;
    const manifestLocaleSet = new Set(locales);
    const outsideManifest = rowLocales.find((locale) => !manifestLocaleSet.has(locale));
    if (outsideManifest) {
      throw new BatchInputError(
        `${label}.locales contains ${outsideManifest} outside manifest locales`,
      );
    }
    const rowSet = new Set(rowLocales);
    const replaceExistingLocales =
      translation.replaceExistingLocales == null
        ? []
        : validateLocales(
            translation.replaceExistingLocales,
            available,
            `${label}.replaceExistingLocales`,
          );
    const replacementSet = new Set(replaceExistingLocales);
    const outsideRow = replaceExistingLocales.find((locale) => !rowSet.has(locale));
    if (outsideRow) {
      throw new BatchInputError(
        `${label}.replaceExistingLocales contains ${outsideRow} outside its scope`,
      );
    }
    for (const [locale, value] of Object.entries(values)) {
      if (!rowSet.has(locale)) {
        throw new BatchInputError(`${label}.values contains locale ${locale} outside its scope`);
      }
      if (typeof value !== "string") {
        throw new BatchInputError(`${label}.values.${locale} must be a string`);
      }
    }
    for (const locale of rowLocales) {
      let value;
      if (Object.hasOwn(values, locale)) {
        value = values[locale];
        authored += 1;
      } else if (translation.copyFrom) {
        value = copiedValue(context, effective.get(locale), locale, translation.copyFrom);
        copied += 1;
      } else {
        throw new BatchInputError(`${label} is missing a value for ${locale}`);
      }
      entries.push({
        catalog: context.id,
        locale,
        key: translation.key,
        value,
        ...((translation.replaceExisting || replacementSet.has(locale)) && {
          replaceExisting: true,
        }),
      });
    }
  }

  return {
    entries: validatePayload(entries),
    manifest: true,
    provenance: { authored, copied },
    review: manifest.review ?? [],
  };
}

export function expandBatch(payload, options = {}) {
  if (Array.isArray(payload)) {
    const entries = resolvedEntries(validatePayload(payload), options);
    return {
      entries,
      manifest: false,
      provenance: { authored: entries.length, copied: 0 },
      review: [],
    };
  }
  if (!isObject(payload)) {
    throw new BatchInputError("payload must be a flat array or batch manifest object");
  }
  return expandManifest(payload, options);
}

function canonicalEntries(entries) {
  return entries
    .map(({ catalog, locale, key, value, replaceExisting }) => ({
      catalog,
      locale,
      key,
      value,
      replaceExisting: Boolean(replaceExisting),
    }))
    .toSorted(
      (left, right) =>
        left.catalog.localeCompare(right.catalog) ||
        left.locale.localeCompare(right.locale) ||
        left.key.localeCompare(right.key) ||
        left.value.localeCompare(right.value),
    );
}

export function batchDigest(entries) {
  return createHash("sha256")
    .update(JSON.stringify(canonicalEntries(entries)))
    .digest("hex");
}

export function scopeRows(scope) {
  if (Array.isArray(scope.groups)) {
    const allLocales = scope.locales ?? [];
    return scope.groups.flatMap((group, index) => {
      if (group.replaceExisting != null && typeof group.replaceExisting !== "boolean") {
        throw new BatchInputError(
          `scope.groups[${index}].replaceExisting must be a boolean when present`,
        );
      }
      if (group.replaceExisting && group.replaceExistingLocales != null) {
        throw new BatchInputError(
          `scope.groups[${index}] cannot combine replaceExisting with replaceExistingLocales`,
        );
      }
      const locales = group.locales ?? allLocales;
      const replaceExistingLocales = group.replaceExistingLocales ?? [];
      if (!Array.isArray(replaceExistingLocales)) {
        throw new BatchInputError(
          `scope.groups[${index}].replaceExistingLocales must be an array when present`,
        );
      }
      const replacementSet = new Set();
      for (const [localeIndex, locale] of replaceExistingLocales.entries()) {
        if (typeof locale !== "string" || locale.trim() === "") {
          throw new BatchInputError(
            `scope.groups[${index}].replaceExistingLocales[${localeIndex}] must be a non-empty string`,
          );
        }
        if (!locales.includes(locale)) {
          throw new BatchInputError(
            `scope.groups[${index}].replaceExistingLocales contains ${locale} outside its scope`,
          );
        }
        if (replacementSet.has(locale)) {
          throw new BatchInputError(
            `scope.groups[${index}].replaceExistingLocales contains duplicate locale ${locale}`,
          );
        }
        replacementSet.add(locale);
      }
      return locales.map((locale) => ({
        catalog: group.catalog ?? scope.catalog ?? "",
        locale,
        key: group.key,
        source: group.source,
        replaceExisting: Boolean(group.replaceExisting || replacementSet.has(locale)),
      }));
    });
  }
  if (Array.isArray(scope.entries)) {
    return scope.entries.map((entry, index) => {
      if (entry.replaceExisting != null && typeof entry.replaceExisting !== "boolean") {
        throw new BatchInputError(
          `scope.entries[${index}].replaceExisting must be a boolean when present`,
        );
      }
      return {
        catalog: entry.catalog ?? scope.catalog ?? "",
        locale: entry.locale,
        key: entry.key,
        source: entry.source,
        replaceExisting: Boolean(entry.replaceExisting),
      };
    });
  }
  throw new BatchInputError("scope must contain an entries or groups array");
}

function pairFor(entry) {
  return `${entry.catalog ?? ""}\0${entry.locale}\0${entry.key}`;
}

export function validateCoverage(entries, scope) {
  const expected = new Map(scopeRows(scope).map((entry) => [pairFor(entry), entry]));
  const actual = new Map(entries.map((entry) => [pairFor(entry), entry]));
  const missing = expected
    .keys()
    .filter((pair) => !actual.has(pair))
    .toArray();
  const extra = actual
    .keys()
    .filter((pair) => !expected.has(pair))
    .toArray();
  const operationMismatches = [...expected]
    .filter(
      ([pair, expectedEntry]) =>
        actual.has(pair) &&
        Boolean(actual.get(pair).replaceExisting) !== expectedEntry.replaceExisting,
    )
    .map(([pair, expectedEntry]) => ({
      pair,
      expected: expectedEntry.replaceExisting ? "replace" : "add",
      actual: actual.get(pair).replaceExisting ? "replace" : "add",
    }));
  return {
    ok: missing.length === 0 && extra.length === 0 && operationMismatches.length === 0,
    missing,
    extra,
    operationMismatches,
  };
}

function canonicalScope(scope) {
  return scopeRows(scope)
    .map(({ catalog, locale, key, source, replaceExisting }) => ({
      catalog,
      locale,
      key,
      source,
      replaceExisting,
    }))
    .toSorted(
      (left, right) =>
        left.catalog.localeCompare(right.catalog) ||
        left.locale.localeCompare(right.locale) ||
        left.key.localeCompare(right.key) ||
        String(left.source).localeCompare(String(right.source)) ||
        Number(left.replaceExisting) - Number(right.replaceExisting),
    );
}

export function approvalDigest(entries, scope, targets = []) {
  return createHash("sha256")
    .update(
      JSON.stringify({
        version: 2,
        entries: canonicalEntries(entries),
        scope: canonicalScope(scope),
        targets: targets.toSorted((left, right) => left.file.localeCompare(right.file)),
      }),
    )
    .digest("hex");
}

export function readScope(scopePath) {
  try {
    return JSON.parse(fs.readFileSync(scopePath, "utf8"));
  } catch (error) {
    throw new BatchInputError(`cannot read scope: ${error.message}`);
  }
}

// Signature-mismatch issues for one batch entry against its catalog's source values.
function signatureIssuesForEntry(context, source, entry) {
  if (!source.has(entry.key)) {
    return [
      {
        catalog: context.id,
        locale: entry.locale,
        key: entry.key,
        code: "orphan-key",
        detail: `absent from ${sourceFile(context)}`,
      },
    ];
  }
  const sourceSignature = signatureFor(context.syntax, source.get(entry.key));
  const targetSignature = signatureFor(context.syntax, entry.value);
  if (sourceSignature.error) {
    return [
      {
        catalog: context.id,
        locale: entry.locale,
        key: entry.key,
        code: "source-parse-error",
        detail: sourceSignature.error,
      },
    ];
  }
  if (targetSignature.error) {
    return [
      {
        catalog: context.id,
        locale: entry.locale,
        key: entry.key,
        code: "parse-error",
        detail: targetSignature.error,
      },
    ];
  }
  const issues = [];
  for (const [code, sourceParts, targetParts] of [
    ["placeholder-mismatch", sourceSignature.placeholders, targetSignature.placeholders],
    ["markup-mismatch", sourceSignature.markup, targetSignature.markup],
  ]) {
    const difference = diffSignature(sourceParts, targetParts, {
      syntax: code === "placeholder-mismatch" ? context.syntax : undefined,
    });
    if (difference.missing.length === 0 && difference.extra.length === 0) continue;
    issues.push({
      catalog: context.id,
      locale: entry.locale,
      key: entry.key,
      code,
      ...difference,
    });
  }
  return issues;
}

export function validateBatchSignatures(entries, options = {}) {
  const repo = detectedRepo(options);
  const byCatalog = new Map();
  for (const entry of entries) {
    const context = resolveCatalog(repo, entry.catalog ?? options.catalog);
    if (context.error) throw new BatchInputError(context.error);
    if (!byCatalog.has(context.id)) byCatalog.set(context.id, { context, entries: [] });
    byCatalog.get(context.id).entries.push(entry);
  }

  const issues = [];
  for (const { context, entries: catalogEntries } of byCatalog.values()) {
    const files = localeFiles(context);
    const { bundles, errors } = loadCatalogBundles(context, translationsDir(context), files);
    const sourceError = errors.get(sourceLocale(context));
    if (sourceError) {
      throw new BatchInputError(`cannot read ${sourceFile(context)}: ${sourceError.error.message}`);
    }
    const source = bundles.get(sourceLocale(context)).parsed.values;
    for (const entry of catalogEntries) {
      issues.push(...signatureIssuesForEntry(context, source, entry));
    }
  }
  return issues;
}

export function validateScopeSources(scope, options = {}) {
  const repo = detectedRepo(options);
  const expected = new Map();
  for (const row of scopeRows(scope)) {
    if (
      !isNonEmptyString(row.catalog) ||
      !isNonEmptyString(row.locale) ||
      !isNonEmptyString(row.key)
    ) {
      throw new BatchInputError("scope rows require non-empty catalog, locale, and key strings");
    }
    if (typeof row.source !== "string") {
      throw new BatchInputError(`scope source must be a string for ${row.key}`);
    }
    const identity = `${row.catalog}\0${row.key}`;
    const previous = expected.get(identity);
    if (previous && previous.source !== row.source) {
      throw new BatchInputError(`scope has conflicting source text for ${row.catalog}:${row.key}`);
    }
    expected.set(identity, row);
  }

  const byCatalog = new Map();
  for (const row of expected.values()) {
    const context = resolveCatalog(repo, row.catalog ?? options.catalog);
    if (context.error) throw new BatchInputError(context.error);
    if (!byCatalog.has(context.id)) byCatalog.set(context.id, { context, rows: [] });
    byCatalog.get(context.id).rows.push(row);
  }

  const issues = [];
  for (const { context, rows } of byCatalog.values()) {
    const files = localeFiles(context);
    const { bundles, errors } = loadCatalogBundles(context, translationsDir(context), files);
    const sourceError = errors.get(sourceLocale(context));
    if (sourceError) {
      throw new BatchInputError(`cannot read ${sourceFile(context)}: ${sourceError.error.message}`);
    }
    const source = bundles.get(sourceLocale(context)).parsed.values;
    for (const row of rows) {
      if (!source.has(row.key)) {
        issues.push({
          catalog: context.id,
          key: row.key,
          code: "orphan-source",
          expected: row.source,
        });
      } else if (source.get(row.key) !== row.source) {
        issues.push({
          catalog: context.id,
          key: row.key,
          code: "source-changed",
          expected: row.source,
          actual: source.get(row.key),
        });
      }
    }
  }
  return issues;
}

export function localeWriteSummary(entries) {
  const counts = new Map();
  for (const entry of entries) {
    const identity = `${entry.catalog}\0${entry.locale}`;
    counts.set(identity, (counts.get(identity) ?? 0) + 1);
  }
  const values = counts.values().toArray();
  const isUniform = values.length > 0 && values.every((count) => count === values[0]);
  return {
    locales: counts.size,
    uniformCount: isUniform ? values[0] : null,
    rows: [...counts].map(([identity, count]) => {
      const [catalog, locale] = identity.split("\0", 2);
      return { catalog, locale, count };
    }),
  };
}
