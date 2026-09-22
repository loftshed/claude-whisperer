# Extraction TODO — generic localization skill

Generic version of the localization skill extracted from the work marketplace
(source, read-only: `/Users/lofstpe1/Documents/claude-plugin-marketplace/plugins/oss-shared/skills/localization/`).
Target: this repo, `skills/localization/`, published later to `loftshed/claude-whisperer`.
Do not push until the remaining items below are done.

## Done

- Copied the 37-file skill skeleton (SKILL.md, references/, scripts/ with lib + geo-names.json).
- `scripts/lib/adapters.mjs` rewritten config-driven: detects `.i18n-catalogs.json`
  at the repo root instead of hardcoded OneSpan package names. Same catalog schema,
  plus root-level `name`, `exclusions`, `brands`, `keepInEnglish`.
- `scripts/lib/content-rules.mjs`: `BRAND_NAMES` removed; `brands` and
  `keepInEnglish` are now parameters (OneSpan brand list and `OSS` gone;
  generic acronym defaults kept). `check-content.mjs` threads them from config
  (also covers preflight's virtual review).
- Vendored git fork-point helper as `scripts/lib/git-fork.mjs` (~90 generic lines
  from the plugin-level shared `git.mjs`); `untranslated.mjs` and
  `validate-structure.mjs` imports repointed.
- `apply-translations.mjs` JSON staging now routes through `sourceFile()` /
  `fileForLocale()` (honors the `extension` field; small latent bug fixed).
- Script cleanups: `detect-context.mjs` hardcoded context line → config hint;
  `create-scratch.mjs` prefix `oss-localization.` → `localization.`; repo-name
  comments and `oss.generic.close` example keys genericized (find-usages,
  locate-key, usage, placeholders).
- `scripts/lib/geo-names.json`: key prefixes `esl.*` → `geo.*`, meta note updated.
- `SKILL.md` fully rewritten: neutral description/frontmatter (oss-plugins cache
  Read allowance dropped), new "Catalog configuration" section, generic format
  references, `localization.XXXXXX` scratch naming, generic examples.
- New references written: `catalog-configuration.md` (schema + 3 example configs),
  `format-json-icu.md`, `format-polyglot-ts.md`, `format-properties.md`
  (generic replacements), `terminology.md` (starter template).

## Remaining

1. **`references/legal-critical-terms.md`** — still the OneSpan e-signature
   content (the starter write was interrupted). Rewrite as a generic starter
   (seed list: agree/consent, delete data, pay/billing, cancel/terminate,
   expire/deadline) with a "tune to your domain" note.
2. **Delete the replaced OneSpan format docs**: `references/format-sender-ui.md`,
   `references/format-admin-ui.md`, `references/format-esl-backend.md`
   (already superseded by format-json-icu / format-polyglot-ts / format-properties).
3. **Genericize residual OneSpan bits** in `references/authority-stack.md`
   (`esl.countries.` example, e-sign example), `references/source-clarity.md`
   (OneSpan product-vocabulary note, "Map"→"Link" example),
   `references/target-language-conventions.md` (OneSpan Sign register note,
   `esl.` namespace references).
4. **Repo-level files**: add `LICENSE` (MIT, © loftshed, CLDR Unicode-3.0
   attribution for geo-names.json) and a README section introducing the skill
   and the `.i18n-catalogs.json` concept.
5. **Verification** (none run yet):
   - `node --check` every `.mjs` under `skills/localization/scripts/`.
   - OneSpan sweep must be zero:
     `rg -in 'onespan|oss[-_. ]|sender-ui|signer-ui|esl\.|OSSQ|digipass' skills/localization/`
   - End-to-end smoke test in a temp dir: fixture repo with `.i18n-catalogs.json`
     (flat ICU json) + git history with cached `refs/remotes/origin/master`, then
     `detect-context` → `untranslated --branch --audit-source` → author
     `batch.json` → `preflight-batch` (get digest) → `apply-translations` →
     `validate-structure --branch`. Also smoke `detect-context` for a
     properties-format fixture.

## Deferred

- Push to `github.com/loftshed/claude-whisperer` (main) — deliberate, per user:
  bank locally first, publish after the remaining items are done.
