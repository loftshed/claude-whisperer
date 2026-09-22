# Terminology: what the product's ambiguous terms actually mean

> Starter template — replace the examples below with this product's real term
> decisions during the first localization pass. An empty glossary is fine; the
> authority stack's tier 0 (corpus consensus) still applies.

Grading a target string against the glossary (tier 2 of `authority-stack.md`) requires knowing what the English term _means in this product_, because several of them collide with a common-language sense that a translator or MT will reach for by default. Use these definitions when the changed string turns on one of these terms, and prefer a target rendering consistent with the meaning below over a literal one.

## Product term meanings

Add one bullet per ambiguous term: **term** — what it means in this product, and which common-language sense to avoid. Example shape (replace):

- **workspace** — a shared container for a team's documents. Not the generic "area" sense, not a desktop environment.

When one of these terms drives the meaning of a changed string, its rendering should match how the same term is rendered elsewhere in the corpus (tier 0). Flag divergence as a review candidate, never a verdict.

## Keep-in-English terms

Two tiers, because they behave differently under review.

**Brand names** — proper nouns that are never correctly translated. List this product's brands in the `brands` field of `.i18n-catalogs.json`; the `check-content.mjs` `brand-drift` check flags a locale that altered or dropped one.

**Technical terms kept in English** — generic acronyms and product identifiers usually left in English (`API`, `REST`, `SMS`, `URL`, `PDF`, `OAuth`, `OAuth 2.0`, `SSO`, `SAML`, plus browser names). These are only an _exclusion_ for the identical-to-source check (a value that is exactly one of them is legitimately identical); they are **not** brand-drift-checked, because locales legitimately localize them. The default list lives in `scripts/lib/content-rules.mjs` (`KEEP_IN_ENGLISH`); override it per repository with the `keepInEnglish` field of `.i18n-catalogs.json`.

Note what is deliberately absent: `Q&A`, `Fast Track`, and similar generic phrases are **not** keep-in-English. Locales correctly localize them (`Q&A` → fr `Q&R`, de `F&A`, es `preguntas de seguridad`), so treating their translation as drift is a false positive.

## Jurisdiction-specific fields

Translate the label, not the legal identity of the data being collected. When a field is jurisdiction-specific (for example, a U.S. Social Security Number), do not substitute the destination locale's national identifier: that changes what the form accepts and can mislead the user. For these fields, render-site semantics outrank an exact corpus match whose wording changes the accepted jurisdiction or identifier.
