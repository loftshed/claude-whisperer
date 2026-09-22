# TypeScript/Polyglot catalog format

Read this reference when the detected catalog uses `format: "typescript"` with `syntax: "polyglot"` (React Admin style).

- Preserve every `%{name}` placeholder in the corresponding plural form.
- Keep the same `||||` plural-form count and order as the English source.
- The writer may replace direct static string literals or insert a static property under the nearest existing object. Generated and shared values are read-only.
- The catalog covers app-owned messages and local overrides. Dependency-provided defaults remain owned by their language packages.
- `targetLocales` lists the locale modules the app owns; only those modules and the source module are written.
- Generated dictionaries configured under `generatedDictionaries` describe helper-generated content; they do not create additional interface locales.
