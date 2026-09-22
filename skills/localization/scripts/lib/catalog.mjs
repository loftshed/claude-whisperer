import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import { fallbackLocales, localeForFile, sourceFile, sourceLocale } from "./adapters.mjs";
import { parseJsonCatalog } from "./json-catalog.mjs";
import { parseProperties } from "./properties.mjs";
import { parseTypeScriptCatalog } from "./typescript-catalog.mjs";

// An optional catalog introduced on this branch has an empty source baseline.
// A failed lookup of the Git reference itself must still surface as an error.
export function sourceAbsentAtReference(catalog, reference) {
  if (!catalog.optional) return false;
  const file = path.posix.join(catalog.dir, sourceFile(catalog));
  const result = spawnSync("git", ["ls-tree", "--name-only", reference, "--", file], {
    cwd: catalog.root,
    encoding: "utf8",
  });
  return result.status === 0 && result.stdout.trim() === "";
}

export function parseCatalogBuffer(catalog, buffer) {
  if (catalog.format === "properties") return parseProperties(buffer);
  if (catalog.format === "typescript") return parseTypeScriptCatalog(buffer, catalog);
  if (catalog.format === "json") return parseJsonCatalog(buffer);
  throw new Error(`unsupported catalog format ${catalog.format}`);
}

export function readCatalogFile(catalog, dir, file) {
  const buffer = fs.readFileSync(path.join(dir, file));
  return { file, buffer, parsed: parseCatalogBuffer(catalog, buffer) };
}

export function loadCatalogBundles(catalog, dir, files) {
  const bundles = new Map();
  const errors = new Map();
  for (const file of files) {
    const locale = localeForFile(catalog, file);
    try {
      bundles.set(locale, readCatalogFile(catalog, dir, file));
    } catch (error) {
      errors.set(locale, { file, error });
    }
  }
  return { bundles, errors };
}

export function effectiveCatalog(catalog, locale, bundles) {
  const values = new Map();
  const owners = new Map();
  const chain = fallbackLocales(catalog, locale);

  for (const candidate of chain.toReversed()) {
    const bundle = bundles.get(candidate);
    if (!bundle) continue;
    for (const [key, value] of bundle.parsed.values.entries()) {
      values.set(key, value);
      owners.set(key, { locale: candidate, file: bundle.file });
    }
  }

  return { chain, values, owners };
}

// Return a rendering that can safely serve as translation evidence or a
// copyFrom source. A non-English locale falling all the way through to the
// English source is still untranslated, while same-language inheritance
// (en-GB -> en) and translated parent inheritance (es-ES -> es) are valid.
export function translatedRendering(catalog, locale, key, effective) {
  const value = effective.values.get(key);
  const owner = effective.owners.get(key);
  const source = sourceLocale(catalog);
  const isInheritedSource =
    owner?.locale === source && locale.split("-", 1)[0] !== source.split("-", 1)[0];
  return typeof value !== "string" || value.trim() === "" || isInheritedSource ? null : value;
}

// Classify a key that is absent from a locale's own bundle against a
// pre-built effective catalog (see effectiveCatalog). Callers build the
// effective catalog once per locale and reuse it across every key, so this
// stays O(1) per key rather than rebuilding the fallback chain each time.
export function classifyAbsence(catalog, locale, key, effective) {
  const owner = effective.owners.get(key);
  const language = locale.split("-", 1)[0];
  const source = sourceLocale(catalog);
  const isInherited = Boolean(
    owner &&
    translatedRendering(catalog, locale, key, effective) !== null &&
    (language === source ||
      (owner.locale !== source && owner.locale.split("-", 1)[0] === language)),
  );

  return {
    explicit: false,
    inherited: isInherited,
    needsTranslation: !isInherited,
    effectiveFrom: owner?.locale ?? null,
  };
}
