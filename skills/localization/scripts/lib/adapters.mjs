// Per-repository i18n configuration. The skill runs inside a consumer repo and
// reads a `.i18n-catalogs.json` file at the repository root; everything that
// differs between repos lives in that file so the rest of the skill stays
// repo-agnostic. A repo exposes one or more named catalogs — independent
// translation stores with their own format and layout — and every script
// resolves exactly one catalog before doing any work. Locales are logical
// hyphenated IDs (es-ES, zh-CN) everywhere outside this module; the helpers
// below own the mapping to on-disk filenames.
//
// Config file fields (`.i18n-catalogs.json`):
//   name          optional repo label shown in reports (defaults to the root
//                 directory's name)
//   catalogs      required, non-empty array of catalog objects (see below)
//   exclusions    optional array of { path, files?, reason } — known
//                 localized-looking resources that are deliberately not
//                 catalogs; detect-context surfaces them as diagnostics
//   brands        optional array of proper-noun brand/product names that are
//                 never correctly translated; drives the brand-drift check
//   keepInEnglish optional array of terms that legitimately ship in English;
//                 replaces the built-in defaults for the identical-source
//                 exclusion (see lib/content-rules.mjs)
//
// Catalog fields:
//   id            stable name, selected with --catalog when a repo has several
//   format        "json" | "typescript" | "properties"; selects the file layout and codec
//   syntax        "sprintf" | "icu" | "polyglot" | "messageformat"
//   keyStyle      "nested" | "flat", selects how translation keys are stored
//   dir           translations dir, relative to the repo root
//   rtlLocales    locales that need a bidi check (informational for now)
//   optional      expose the catalog only when its directory exists in this checkout
//   sourceNotes   source coverage details shown by detect-context
//
// json catalogs ("<locale>.json" per locale):
//   sourceLocale  the authored source file; the parity baseline
//   extension     optional explicit extension (defaults to ".json")
//   ignore        files in the translations dir that are not locales
//   geoKeyPrefixes  key prefixes for geographic proper nouns (countries, states,
//                   provinces) that are intentionally kept identical across many
//                   locales; the identical-to-source content check excludes them
//                   and, when the bundled CLDR reference table covers the
//                   prefix, the geo-mismatch audit runs. Use the shipped
//                   "geo.countries." style namespaces to match the bundled table.
//   messageIndirection  resolve defineMessages property names and imported descriptors
//                   to their catalog IDs when finding render sites and branch usage.
//
// TypeScript catalogs ("<locale>.ts" per locale):
//   targetLocales   locale modules owned by the app
//   ignoreKeyPrefixes  metadata paths excluded from the message catalog
//   generatedDictionaries  static helper calls (object keys) and their generated
//                 suffixes (arrays of locale codes)
//   extension     optional explicit extension (defaults to ".ts")
//
// properties catalogs (Java ResourceBundle layout):
//   source        { locale, file }: the suffixless base bundle is the English
//                 source, so its logical locale is configured, not parsed
//   targets       { basename, separator, extension, locales }: target files are
//                 basename + separator + underscore-suffixed locale + extension,
//                 restricted to the allowlist of shipped locales; stray
//                 suffixes are not locales

import fs from "node:fs";
import path from "node:path";

const CONFIG_FILENAME = ".i18n-catalogs.json";

const FORMATS = new Set(["json", "typescript", "properties"]);
const SYNTAXES = new Set(["sprintf", "icu", "polyglot", "messageformat"]);
const KEY_STYLES = new Set(["nested", "flat"]);

const FORMAT_EXTENSIONS = { json: ".json", typescript: ".ts" };

function configCandidates(startDir) {
  const candidates = [];
  let dir = path.resolve(startDir);
  while (true) {
    const candidate = path.join(dir, CONFIG_FILENAME);
    if (fs.existsSync(candidate)) candidates.push(candidate);
    const parent = path.dirname(dir);
    if (parent === dir) return candidates;
    dir = parent;
  }
}

function packageJsonName(dir) {
  const packagePath = path.join(dir, "package.json");
  try {
    return JSON.parse(fs.readFileSync(packagePath, "utf8")).name ?? null;
  } catch {
    return null;
  }
}

function nonEmptyString(value) {
  return typeof value === "string" && value.trim() !== "";
}

// Return a human-readable reason when the config is invalid, else null.
export function validateConfig(config) {
  if (typeof config !== "object" || config === null || Array.isArray(config)) {
    return "expected a JSON object";
  }
  if (!Array.isArray(config.catalogs) || config.catalogs.length === 0) {
    return "catalogs must be a non-empty array";
  }
  for (const catalog of config.catalogs) {
    if (typeof catalog !== "object" || catalog === null) return "each catalog must be an object";
    const where = `catalog ${nonEmptyString(catalog.id) ? catalog.id : "(missing id)"}`;
    if (!nonEmptyString(catalog.id)) return `${where}: id is required`;
    if (!FORMATS.has(catalog.format)) {
      return `${where}: format must be one of ${[...FORMATS].join(", ")}`;
    }
    if (!SYNTAXES.has(catalog.syntax)) {
      return `${where}: syntax must be one of ${[...SYNTAXES].join(", ")}`;
    }
    if (!KEY_STYLES.has(catalog.keyStyle)) {
      return `${where}: keyStyle must be one of ${[...KEY_STYLES].join(", ")}`;
    }
    if (!nonEmptyString(catalog.dir)) return `${where}: dir is required`;
    if (catalog.format === "properties") {
      if (
        typeof catalog.source !== "object" ||
        catalog.source === null ||
        !nonEmptyString(catalog.source.locale) ||
        !nonEmptyString(catalog.source.file)
      ) {
        return `${where}: properties catalogs need source { locale, file }`;
      }
      const targets = catalog.targets;
      if (
        typeof targets !== "object" ||
        targets === null ||
        !nonEmptyString(targets.basename) ||
        !nonEmptyString(targets.separator) ||
        !nonEmptyString(targets.extension) ||
        !Array.isArray(targets.locales) ||
        targets.locales.some((locale) => !nonEmptyString(locale))
      ) {
        return `${where}: properties catalogs need targets { basename, separator, extension, locales }`;
      }
    } else if (!nonEmptyString(catalog.sourceLocale)) {
      return `${where}: ${catalog.format} catalogs need sourceLocale`;
    }
  }
  return null;
}

// Resolve the i18n config for the repo containing startDir. Returns
// { repo, catalogs, root, pkgName }, or { repo: "unknown", ... } with a reason
// the caller can surface.
export function detectRepo(startDir = process.cwd()) {
  const candidates = configCandidates(startDir);
  if (candidates.length === 0) {
    return {
      repo: "unknown",
      reason: `no ${CONFIG_FILENAME} found walking up from cwd`,
    };
  }

  const configPath = candidates[0];
  let config;
  try {
    config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  } catch (error) {
    return { repo: "unknown", reason: `could not parse ${configPath}: ${error.message}` };
  }

  const failure = validateConfig(config);
  if (failure) {
    return { repo: "unknown", reason: `${configPath}: ${failure}` };
  }

  const root = path.dirname(configPath);
  return {
    repo: nonEmptyString(config.name) ? config.name : path.basename(root),
    catalogs: config.catalogs.filter(
      (catalog) => !catalog.optional || fs.existsSync(path.join(root, catalog.dir)),
    ),
    root,
    pkgName: packageJsonName(root),
    ...(config.exclusions !== undefined && { exclusions: config.exclusions }),
    ...(config.brands !== undefined && { brands: config.brands }),
    ...(config.keepInEnglish !== undefined && { keepInEnglish: config.keepInEnglish }),
  };
}

// Resolve one catalog from a detected repo config. With an id, that catalog is
// required to exist; without one, the repo must have exactly one catalog. The
// resolved catalog carries the repo-level fields the scripts need (root, repo,
// brands, keepInEnglish), or { error } for the caller to surface with its own
// error model.
export function resolveCatalog(context, id = null) {
  const catalogs = context.catalogs ?? [];
  const ids = catalogs.map((catalog) => catalog.id).join(", ");
  const repoFields = {
    root: context.root,
    repo: context.repo,
    catalogDirs: catalogs.map((catalog) => catalog.dir),
    ...(context.brands !== undefined && { brands: context.brands }),
    ...(context.keepInEnglish !== undefined && { keepInEnglish: context.keepInEnglish }),
  };
  if (id !== null && id !== undefined) {
    const found = catalogs.find((catalog) => catalog.id === id);
    if (!found) {
      return { error: `unknown catalog ${id}; ${context.repo} has: ${ids || "(none)"}` };
    }
    return { ...found, ...repoFields };
  }
  if (catalogs.length === 1) {
    return { ...catalogs[0], ...repoFields };
  }
  return {
    error: `${context.repo} has ${catalogs.length} catalogs (${ids || "none"}); pass --catalog <id>`,
  };
}

// Absolute path to the translations dir for a resolved catalog.
export function translationsDir(catalog) {
  return path.join(catalog.root, catalog.dir);
}

// Filename of the catalog's source (parity baseline) file.
export function sourceFile(catalog) {
  return catalog.format === "properties" ? catalog.source.file : catalog.sourceLocale;
}

// Logical source locale for a resolved catalog.
export function sourceLocale(catalog) {
  if (catalog.format === "properties") return catalog.source.locale;
  const extension = catalog.extension ?? FORMAT_EXTENSIONS[catalog.format];
  return catalog.sourceLocale.endsWith(extension)
    ? catalog.sourceLocale.slice(0, -extension.length)
    : catalog.sourceLocale;
}

// Runtime lookup order for one locale. Missing intermediate bundles remain in
// the chain so callers can continue to the source bundle when no file exists.
export function fallbackLocales(catalog, locale) {
  if (catalog.format !== "properties") return [locale];

  const chain = [];
  const parts = locale.split("-");
  while (parts.length > 0) {
    chain.push(parts.join("-"));
    parts.pop();
  }
  const source = sourceLocale(catalog);
  if (!chain.includes(source)) chain.push(source);
  return chain;
}

// Logical hyphenated locale for a file in the catalog, or null when the file
// does not name one of the catalog's locales.
export function localeForFile(catalog, file) {
  if (catalog.format === "properties") {
    if (file === catalog.source.file) return catalog.source.locale;
    const { basename, separator, extension } = catalog.targets;
    const prefix = basename + separator;
    if (!file.startsWith(prefix) || !file.endsWith(extension)) return null;
    const locale = file.slice(prefix.length, -extension.length).replaceAll("_", "-");
    return catalog.targets.locales.includes(locale) ? locale : null;
  }
  const extension = catalog.extension ?? FORMAT_EXTENSIONS[catalog.format];
  if (!file.endsWith(extension)) return null;
  const locale = file.slice(0, -extension.length);
  if (
    catalog.targetLocales &&
    locale !== sourceLocale(catalog) &&
    !catalog.targetLocales.includes(locale)
  ) {
    return null;
  }
  return locale;
}

// Filename for a logical locale in the catalog.
export function fileForLocale(catalog, locale) {
  if (catalog.format === "properties") {
    if (locale === catalog.source.locale) return catalog.source.file;
    const { basename, separator, extension } = catalog.targets;
    return `${basename}${separator}${locale.replaceAll("-", "_")}${extension}`;
  }
  const extension = catalog.extension ?? FORMAT_EXTENSIONS[catalog.format];
  return `${locale}${extension}`;
}

// Locale files in a resolved catalog's translations dir, sorted. json: every
// file with the format's extension minus the ignore list; properties: the
// source file plus the allowlisted target filenames that exist. Throws if the
// dir does not exist.
export function localeFiles(catalog) {
  const dir = translationsDir(catalog);
  if (catalog.format === "properties") {
    const expected = new Set([
      sourceFile(catalog),
      ...catalog.targets.locales.map((locale) => fileForLocale(catalog, locale)),
    ]);
    return fs
      .readdirSync(dir)
      .filter((f) => expected.has(f))
      .sort((a, b) => (a > b) - (a < b));
  }
  const extension = catalog.extension ?? FORMAT_EXTENSIONS[catalog.format];
  const entries = fs.readdirSync(dir);
  if (catalog.targetLocales) {
    const requiredTargets = catalog.targetLocales.map((locale) => fileForLocale(catalog, locale));
    const missingTargets = requiredTargets.filter((file) => !entries.includes(file));
    if (missingTargets.length > 0) {
      throw new Error(`required target locale file(s) missing: ${missingTargets.join(", ")}`);
    }
  }
  const expected = catalog.targetLocales
    ? new Set([
        sourceFile(catalog),
        ...catalog.targetLocales.map((locale) => fileForLocale(catalog, locale)),
      ])
    : null;
  return entries
    .filter(
      (f) =>
        f.endsWith(extension) &&
        !(catalog.ignore ?? []).includes(f) &&
        (expected === null || expected.has(f)),
    )
    .sort((a, b) => (a > b) - (a < b));
}
