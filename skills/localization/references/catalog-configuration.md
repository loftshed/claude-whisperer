# Catalog configuration (`.i18n-catalogs.json`)

The skill discovers a consumer repository's catalogs from a `.i18n-catalogs.json` file at the repository root (detected by walking up from the working directory, like `package.json`). Commit the file so setup happens once per repository.

## Root fields

| Field | Required | Purpose |
| --- | --- | --- |
| `name` | no | Repo label shown in reports; defaults to the root directory's name |
| `catalogs` | yes | Non-empty array of catalog objects (below) |
| `exclusions` | no | Array of `{ path, files?, reason }` — localized-looking resources that are deliberately not catalogs; `detect-context.mjs` surfaces them as diagnostics |
| `brands` | no | Proper-noun brand/product names that are never correctly translated; drives the `brand-drift` content check |
| `keepInEnglish` | no | Terms that legitimately ship in English; replaces the built-in identical-source exclusion defaults (see `terminology.md`) |

## Catalog fields

Common to every format:

| Field | Required | Purpose |
| --- | --- | --- |
| `id` | yes | Stable name; select with `--catalog <id>` when a repo has several |
| `format` | yes | `json` \| `typescript` \| `properties` — selects the file layout and codec |
| `syntax` | yes | `sprintf` \| `icu` \| `polyglot` \| `messageformat` — placeholder dialect |
| `keyStyle` | yes | `nested` \| `flat` — how translation keys are stored |
| `dir` | yes | Translations directory, relative to the repo root |
| `rtlLocales` | no | Locales that need a bidi check (informational) |
| `optional` | no | Expose the catalog only when `dir` exists in this checkout |
| `sourceNotes` | no | Source-coverage details shown by `detect-context.mjs` |

Per format:

- **json** (`<locale>.json` per locale): `sourceLocale` — the authored source file and parity baseline (e.g. `en.json`); `ignore` — non-locale files in `dir`; `geoKeyPrefixes` — key prefixes for geographic proper nouns kept identical across locales (use the `geo.` namespaces to match the bundled CLDR reference table, see `target-language-conventions.md`); `messageIndirection` — resolve `defineMessages` property names and imported descriptors to catalog IDs when finding render sites; `extension` — override the default `.json`.
- **typescript** (`<locale>.ts` per locale): `sourceLocale` (e.g. `en.ts`); `targetLocales` — locale modules the app owns; `ignoreKeyPrefixes` — metadata paths excluded from the catalog; `generatedDictionaries` — helper call names mapped to their generated locale-code suffixes; `extension` — override the default `.ts`.
- **properties** (Java ResourceBundle layout): `source` — `{ locale, file }`; the suffixless base bundle is the English source, so its logical locale is configured, not parsed. `targets` — `{ basename, separator, extension, locales }`: target files are `basename` + `separator` + underscore-suffixed locale + `extension`, restricted to the allowlist of shipped locales; stray suffixes are not locales.

## Examples

React Intl ICU flat JSON:

```json
{
  "name": "web-app",
  "catalogs": [
    {
      "id": "app-messages",
      "format": "json",
      "syntax": "icu",
      "keyStyle": "flat",
      "dir": "src/i18n/translations",
      "sourceLocale": "en.json",
      "ignore": ["missing.json"],
      "rtlLocales": ["ar"],
      "geoKeyPrefixes": ["geo.countries.", "geo.us_states.", "geo.ca_provinces."],
      "messageIndirection": true
    }
  ],
  "brands": ["Example Product"],
  "keepInEnglish": ["API", "OAuth", "SSO"]
}
```

React Admin Polyglot TypeScript modules:

```json
{
  "catalogs": [
    {
      "id": "app-messages",
      "format": "typescript",
      "syntax": "polyglot",
      "keyStyle": "nested",
      "dir": "src/i18nProvider/messages",
      "sourceLocale": "en.ts",
      "targetLocales": ["fr"],
      "ignoreKeyPrefixes": ["meta"],
      "generatedDictionaries": { "languageDictionary": ["en", "de", "fr"] }
    }
  ]
}
```

Java properties with MessageFormat:

```json
{
  "catalogs": [
    {
      "id": "notification-messages",
      "format": "properties",
      "syntax": "messageformat",
      "keyStyle": "flat",
      "dir": "server/src/main/resources",
      "source": { "locale": "en", "file": "notification-messages.properties" },
      "targets": {
        "basename": "notification-messages",
        "separator": "_",
        "extension": ".properties",
        "locales": ["de", "es", "fr", "ja"]
      },
      "rtlLocales": ["ar"]
    }
  ],
  "exclusions": [
    {
      "path": "server/legacy/src/main/resources",
      "files": "messages*.properties",
      "reason": "locale bundles intentionally inherit the default bundle"
    }
  ]
}
```

Verify detection from the repository root with `node <skill>/scripts/detect-context.mjs`; it prints each detected catalog, its source file, and its locale files.
