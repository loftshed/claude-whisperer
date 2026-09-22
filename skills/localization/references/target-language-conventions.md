# Target-language conventions

Criterion 2 is "makes sense in every language". Beyond the term choices in `authority-stack.md` and `terminology.md`, a target string can be accurate yet still wrong for the register or the language's UI conventions. These are the recurring ones for this product's shipped locales. Apply them as review candidates on the changed target strings, never as hard verdicts.

## Register: formal, for every locale

OneSpan Sign is enterprise B2B software, so the formal register is the correct one wherever a language distinguishes:

- **French** — `vous`, never `tu`.
- **German** — `Sie`, never `du`.
- Same for the polite form in other T–V languages (Spanish `usted` contexts, etc.).

A changed string that switched to the informal form is a candidate, even when the wording is otherwise fine.

## UI-form conventions

The grammatical form a language uses for a UI element differs from English and from element type to element type:

- **Buttons** — Spanish uses the infinitive (`Reemplazar`), not the imperative (`Reemplaza`). Many languages follow the same infinitive-for-buttons convention.
- **Labels / status / titles** — Japanese (and other East-Asian UI) prefers a noun phrase over a verb sentence: "Link Expired" → `リンクの有効期限切れ` (noun), not a full `…になりました` sentence.
- **Confirmations** — keep them as a direct question with the language's own question punctuation.

Match the form the rest of the corpus uses for the same element type (tier 0).

## Proper nouns and place names

Country, state, and province names are proper nouns whose target form is not derivable: some are translated (French "Allemagne", Spanish "Canadá"), many stay in English, and which is which varies by name and by locale. Do not translate them part by part, and do not assume they stay English.

They live under the `esl.countries.`, `esl.us_states.`, and `esl.ca_provinces.` keys, and they are the one class where the shipped corpus is the least reliable authority — the entries are bulk-seeded and rarely reviewed. So `check-content.mjs` audits them against a bundled per-locale reference (Unicode CLDR, in `scripts/lib/geo-names.json`) instead of trusting the corpus. It raises a `geo-mismatch` candidate when a value is left in English although the locale has a real translated name (`untranslated`), or when a value differs from the reference name (`diverges`). Both are advisory: CLDR carries its own choices and the product may deviate deliberately, so a human confirms.

Weigh the two reasons differently. `untranslated` is high-precision — a locale that renders "Cambodia" as "Cambodia" while the language has its own name for it is almost always a real miss. `diverges` has a known legitimate class: when the English source uses a long-form or ISO-style name ("Russian Federation", "Korea, Republic of"), a faithful translation of that long form differs from CLDR's short common name ("Россия", "Südkorea") without being wrong. Confirm a `diverges` candidate only when the value differs from both a faithful rendering of the source and the reference name.

Country names have full CLDR coverage. State and province names do not — CLDR carries almost no subdivision translations for the shipped locales, so the audit defers on those keys and they fall back to the keep-in-English default plus judgment.

Country and jurisdiction names in signing, consent, or governing-law copy are also `legal-critical` (see `legal-critical-terms.md`): surface a changed one for review even when its rendering looks right.

## Typography

- **French** — a space precedes `?`, `!`, `:`, `;` (a narrow no-break space in the ideal). "Voulez-vous continuer ?", not "continuer?".
- **`„…"` quote languages** — Czech, German, Polish, Romanian, Slovak, Croatian, Slovenian and similar use `„…"` (`„` = U+201E opening, `"` = U+201C closing), not ASCII `"` or straight `'`. A changed string in these locales that used ASCII quotes is a typography candidate.

Typography is deterministic enough to promote into `check-content.mjs` later; for now it stays a judgment note on the changed set.
