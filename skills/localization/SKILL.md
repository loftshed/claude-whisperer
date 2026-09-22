---
name: localization
allowed-tools: Bash(git:*), Bash(node:*), Bash(rg:*), Write(/tmp/**), Write(/private/tmp/**), Write(/var/folders/**)
description: Translate and localize new or changed English strings across the locale catalogs a repository declares in its .i18n-catalogs.json (React Intl ICU JSON, TypeScript Polyglot, or Java properties MessageFormat), including strings that exist only in the English source. Use during review of user-facing string changes, when adding or editing locale catalogs, and for i18n/l10n QA, localization, translation quality, placeholder parity, terminology consistency, or questions such as whether translations make sense across shipped languages. Without arguments it defaults to the strings added or used on the current branch. Generates grounded translations behind one digest-bound batch-confirmation gate, then validates placeholder and markup parity, parse validity, source clarity, and terminology.
---

# Localization

Localize one coherent branch-scoped batch. Locale catalogs remain read-only until the user approves a preflighted batch. `apply-translations.mjs` is the only sanctioned locale-catalog writer; never edit a locale catalog directly.

Verify Node.js 22.18 or newer with `node --version` and Git with `git --version` before discovery. Install missing tools from [nodejs.org](https://nodejs.org/en/download) and [git-scm.com](https://git-scm.com/downloads), including Git for Windows on native Windows, then rerun the version checks.

## Catalog configuration

The scripts discover the consumer repository's catalogs from a `.i18n-catalogs.json` file at the repository root. If it is missing or invalid, help the user create one by following [the catalog configuration reference](references/catalog-configuration.md): it lists each catalog's `id`, `format` (`json`, `typescript`, or `properties`), placeholder `syntax` (`icu`, `sprintf`, `polyglot`, or `messageformat`), `keyStyle`, translations `dir`, source locale, and shipped target locales, plus optional repository-level `brands` and `keepInEnglish` term lists. Commit the file to the repository so the setup happens once.

## Non-negotiable flow

1. Discover `fork-point..HEAD` with `--branch --audit-source`; never compare the feature branch directly with today's default-branch tip.
2. Investigate non-rendered keys and pause on a confirmed dead, contradictory, or defective source string.
3. Gather one compact evidence report and reconcile exact-match consensus before authoring.
4. Stage one coherent manifest; use `copyFrom` only for inspected, exact-English, same-meaning anchors.
5. Run `preflight-batch.mjs` once and obtain explicit approval for its ready digest.
6. Apply only with that digest, then run the branch-attributed structural gate.

The scripts read `.i18n-catalogs.json` and select the catalog, source locale, shipped targets, fallback rules, syntax, and writer:

!`node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/detect-context.mjs"`

If detection is empty or `unknown`, fix or create the consumer repository's `.i18n-catalogs.json` first, then run the detector from the repository root. Run the rest of the workflow there.

Before discovery, read the format reference matching the detected catalog: [the JSON/ICU format reference](references/format-json-icu.md), [the TypeScript/Polyglot format reference](references/format-polyglot-ts.md), or [the Java properties format reference](references/format-properties.md).

From the consumer repository, create one unique scratch directory **outside the repository** before running the workflow:

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/create-scratch.mjs"
```

The helper uses the operating system's temporary folder and refuses a folder inside the consumer repository, including symlink targets. Record the printed absolute path and replace `/absolute/external/localization.XXXXXX` in every command below with that literal path, keeping its surrounding quotes. Keep `scope.json`, `evidence.json`, and `batch.json` there; never stage workflow artifacts in the repository, where an untracked artifact could be mistaken for branch code by a later discovery run.

If the temporary folder is unavailable or inside the repository, choose an existing external folder and rerun with `--temp-dir "<absolute external folder>"`.

## 1. Discover the developer's scope and audit the source

A bare skill invocation means the current branch only:

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/untranslated.mjs" --branch --audit-source --group-by-key --out "/absolute/external/localization.XXXXXX/scope.json"
```

`--branch` does **not** fetch, contact the live remote, diff against today's default-branch tip, use a movable local `master`/`main`, or use the feature branch's upstream as the comparison endpoint. It reads the developer clone's locally cached remote-tracking default ref (normally `refs/remotes/origin/master` or `refs/remotes/origin/main`), computes `git merge-base HEAD <cached-remote-ref>`, and inspects `fork-point..HEAD` plus uncommitted and untracked work. The cached remote ref only locates the fork point; the fork commit is the immutable comparison base for the run. This keeps later upstream activity and unrelated keys out of a merge-request review. The summary names both the cached ref and fork commit. A suspicious `origin/HEAD` that points at the current feature branch is ignored when one unambiguous conventional default ref exists; ambiguous or untrustworthy refs fail closed and require an explicit `--since`.

Use `--since <ref>` only when the user explicitly supplies a different comparison base. Widen to the whole untranslated corpus only on an explicit whole-corpus request. Never silently widen when Git or catalog detection fails.

The grouped report states the locale set once and one row per source key. Read the compact stdout summary; do not dump `scope.json` into the conversation. For a changed English value, discovery compares each direct locale value with that locale file at the fork point: an unchanged existing value is marked `replaceExisting: true`, a missing value is an add, and a locale already changed on the branch is complete and omitted. Mixed operations appear as `replaceExistingLocales`. This makes a post-apply discovery idempotent instead of reopening every changed-source key. `source audit` classifies each pending key as rendered, definition-only, test-only, or unreferenced and lists literal exact-English peers. Its working-tree scan includes tracked and untracked render sites.

Investigate every non-rendered key before translating. Use the compact usage result first, then `--verbose` or one targeted `rg` only when necessary. One conclusive usage result is enough: do not print whole source files, repeat repository-wide searches, or inspect the localization scripts themselves to reconfirm the same classification. If a key is confirmed dead/unwired, misnamed, contradictory to its UI field, or otherwise defective English source, report the defect and pause translation until the source is corrected or the user explicitly chooses to retain it. A confirmed dynamic reference is not a defect; note it and proceed.

Reviewing rendered strings and rewording them is the normal second act of a localization task, and a key this branch both added and later reworded is invisible to discovery: it has locale values, and it has no fork-point English to compare them against. Regenerate the staged scope with a repeated `--restage-key <full.key>` for each such key. Discovery then lists it with its full locale set as `replaceExisting`, and the ordinary preflight-and-apply flow rewrites those values. Never delete catalog entries to force a key back into scope, and keep `--force` out of it. A key whose English changed on the branch after being added upstream needs nothing: discovery already marks it a replacement.

If the user explicitly chooses to skip an audited key, regenerate the staged scope with the same discovery command plus a repeated `--exclude-key <full.key>` for each skipped key. The summary records the exclusion and recalculates its totals. Never hand-edit `scope.json` or write an ad hoc script to filter it. Use `untranslated.mjs --help` for supported discovery flags instead of probing invalid arguments or reading implementation code.

If discovery is empty and the request includes QA, run the structural gate with `--branch` so pre-existing findings stay distinguished from branch findings.

## 2. Gather compact grounding evidence

Before fresh authoring, read:

- `references/authority-stack.md`
- `references/terminology.md`
- `references/target-language-conventions.md`

Read `references/legal-critical-terms.md` when the scope touches legal, consent, identity, or jurisdiction-sensitive language. Read `references/source-clarity.md` only when the source audit raises clarity concerns. For a `typescript` or `properties` catalog, also read its format reference if you have not already.

Retrieve translation memory and same-component wording once:

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/find-translations.mjs" --report "/absolute/external/localization.XXXXXX/scope.json" --fallback-fuzzy --siblings --limit 1 --threshold 0.7 --out "/absolute/external/localization.XXXXXX/evidence.json"
```

The conversation output is a compact exact/fuzzy/fresh triage. The staged JSON contains full locale vectors. Keep that JSON as machine data; do not print it wholesale or probe its shape with Python/Node snippets. Exact `copyFrom` rows need no locale-vector dump. When one or more fuzzy anchors need their locale wording, parse them into a bounded text matrix in one command:

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/render-evidence.mjs" "/absolute/external/localization.XXXXXX/evidence.json" --key <pending-key> [--key <pending-key> ...]
```

Add repeated `--locale <name>` only for a targeted language review. Select all related pending keys in one invocation rather than rerunning a JSON query per key.

Fresh authoring often has to match corpus wording held by keys outside the pending set: the neighboring tooltip a new string extends, the noun a sibling already uses. Read that anchor set across every locale in one command, never by probing catalog files by hand:

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/locate-key.mjs" <anchor.key> [<anchor.key> ...] --all
```

Each key reports its value and `file:line` per locale file. Keep the set to the anchors the new strings actually reuse.

Literal exact-English evidence is consensus-based across every matching key before the display limit. Case-folded, placeholder-normalized, or markup-normalized similarities remain fuzzy evidence and can never authorize `copyFrom`:

- A unanimous exact match with the same UI meaning can be reused.
- A dominant rendering is evidence, not automatic truth.
- Any per-locale conflict is a review candidate; inspect the competing keys and usage context.
- Equivalent anchors with identical locale vectors are collapsed.
- Never copy the first match merely because it appears first; shipped typos and context errors must not propagate.

If an otherwise exact anchor has a clear corpus-quality defect in one locale, retain `copyFrom` for
the sound locales and override the defective locale under `values`. Add a manifest review entry
tagged `corpus-quality` that names the anchor and correction. Leave the existing anchor untouched;
report it as follow-up debt instead of widening the feature's batch.

Same-namespace siblings establish component grammar such as required/optional forms. Fuzzy matches guide terminology but are never exact-copy directives.

The discovery audit already resolves usage status. For remaining semantic ambiguity, query all affected keys together:

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/find-usages.mjs" <key> [<key> ...]
```

Default output is one classified location per key. Add `--verbose` only for the small ambiguous subset that needs source context.

Author the complete source set and locale set together. Do not split locales, language families, or review passes between agents. Preserve all placeholders, selectors, plural forms, interpolation types, and markup. ICU catalogs use ICU arguments, plural/select forms, and rich-text tags. Preserve exact-number selectors such as `=0` and `=1`, and account for the target language's plural categories.

## 3. Stage one compact batch manifest

Prefer a manifest over a flat `keys × locales` array:

```json
{
  "catalog": "app-messages",
  "locales": ["de", "fr"],
  "translations": [
    {
      "key": "new.exactKey",
      "copyFrom": "existing.exactKey"
    },
    {
      "key": "new.freshKey",
      "values": {
        "de": "Neue Übersetzung",
        "fr": "Nouvelle traduction"
      }
    },
    {
      "key": "changed.existingKey",
      "replaceExisting": true,
      "values": {
        "de": "Aktualisierte Übersetzung",
        "fr": "Traduction mise à jour"
      }
    }
  ],
  "review": [
    {
      "key": "new.freshKey",
      "tag": "legal-critical",
      "reason": "Consent wording needs human review"
    }
  ]
}
```

`copyFrom` is expanded from shipped effective locale values and is accepted only when source and destination have identical English text. Add `values` beside it for deliberate locale overrides. It refuses blank or English-fallback renderings. Use fresh `values` for fuzzy matches and changed meanings. A row-level `locales` array may narrow a key but cannot exceed the manifest locale set.

Copy each discovery row's operation into the manifest: rows marked `replaceExisting: true` must retain that flag; ordinary new/missing keys must not add it. When only some locales for one key are replacements, copy the row's `replaceExistingLocales` array into that translation entry. The preflight rejects add/replace mismatches. These scoped flags permit overwriting only the changed-source key/locale pairs discovered for this batch; `--force` remains outside the standard workflow.

Write the manifest directly to `/absolute/external/localization.XXXXXX/batch.json`, not the repository. Do not create a generator program merely to serialize freshly authored translations: it adds scaffolding and another diagnostic surface without reducing the translation work. Use a helper only when values are mechanically derived rather than authored.

Flat `{ catalog, locale, key, value }` arrays remain accepted for compatibility, but do not repeat exact shipped locale vectors when `copyFrom` expresses the same evidence safely.

## 4. Run the one approval preflight

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/preflight-batch.mjs" --scope "/absolute/external/localization.XXXXXX/scope.json" "/absolute/external/localization.XXXXXX/batch.json"
```

This single read-only gate:

- expands exact-copy directives;
- requires exact key/locale/catalog and add/replace coverage of discovery scope;
- verifies that each current English source still equals the source captured by discovery;
- validates placeholder, markup, and message syntax parity;
- simulates the real writer, including orphan/overwrite safety;
- rejects duplicate JSON keys, including duplicate parent objects, and target paths that alias an English source or another locale catalog;
- runs terminology and content candidates against the proposed in-memory values;
- prints compact totals and review candidates; and
- emits a SHA-256 digest bound to the expanded entries, discovery scope and source text, and every target file's full original contents, logical path, and resolved physical path, but only when every blocking check is ready.

Do not separately render the matrix, dry-run the writer, or run whole-corpus advisory checks during generation. The bounded human preflight samples every non-empty review category. Use `--json` only when a blocking detail is absent from that sample, and never dump the JSON report into the conversation; extract only the specific unresolved candidate. If any preflight input or touched target catalog changes, rerun preflight and obtain approval for the new digest. This includes edits to unrelated keys or formatting in a touched target file.

Ask once for explicit approval of the ready digest. Include counts, legal-critical items, confirmed source concerns, and genuinely low-confidence choices—not the full locale matrix unless requested. Stop until approval.

## 5. Apply exactly what was approved

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/apply-translations.mjs" --scope "/absolute/external/localization.XXXXXX/scope.json" --approved-digest SHA256_FROM_PREFLIGHT "/absolute/external/localization.XXXXXX/batch.json"
```

The CLI refuses a real write without both the scope and digest. Before writing, it rechecks coverage, operations, English sources, message syntax, and target-file safety, then recomputes the approval digest using the current target contents and paths. It retains JSON indentation, newline style, and existing key order, inserts new keys among their natural-sort neighbors, changes only static writable TypeScript expressions, and preserves untouched properties bytes. Preflight and apply report existing ordering problems under `key order:`; run the consumer repo's formatter or order lint when reported. Existing non-empty values are refused unless that exact discovery row carries `replaceExisting: true`; keep `--force` outside the standard flow.

Then run the structural gate once:

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/validate-structure.mjs" --branch
```

In branch mode the human report details branch-introduced failures and summarizes pre-existing failures without expanding them. Use `--show-pre-existing` only when the user asks to investigate those unrelated findings; `--json` remains available for machine diagnostics. Fix any branch-introduced placeholder, markup, parse, or duplicate-key failure. A changed proposal needs a new preflight digest and approval.

If the repository's format reference documents consumer-owned validators, run them alongside the structural gate.

## Standalone structural and review checks

For validation without generation:

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/validate-structure.mjs" --json [--branch | --since <ref>] [--locale <name>] [--strict]
```

Exit `0` is structurally clean, `1` means findings, and `2` means invalid source/configuration. Normal mode gates placeholder/markup/parse/duplicate failures; `--strict` also gates stale keys. Branch attribution uses the same fork-point resolver as discovery.

For a standalone QA request, scope findings to changed keys and use:

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/check-terminology.mjs" [--json] [--locale <name>]
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/check-content.mjs" [--json] [--locale <name>]
```

These are advisory candidates, not verdicts. During generation their virtual, batch-scoped equivalents already run in preflight.

Resolve one or more dotted keys to their source location with:

```bash
node "${CLAUDE_PLUGIN_ROOT}/skills/localization/scripts/locate-key.mjs" <object.path> [<object.path> ...] [--locale <name>] [--all]
```

## Report

Lead with what was written or why the source paused the workflow. State the fork-point scope, keys/locales translated, approved digest, and structural result. Finish with a short confidence-ranked review queue. Scripts own deterministic scope, evidence, write safety, and syntax checks; the active agent owns contextual translation, terminology, grammar, register, regional meaning, and source clarity as one coherent batch.
