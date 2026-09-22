# Authority stack: how to grade a translation's "sense"

Machine translation is the baseline. "Not dumb" means checking a chosen string against how comparable, professional software localizes the **same action in the same context**, and preferring the idiomatic term over the merely literal one. This is the engine for criterion 2 (does it make sense in every language).

Grade each changed target string against these tiers, in order. Where it lands sets its confidence. **Emit review candidates, never verdicts.** You are not authorized to declare a human translation "wrong"; you flag low-confidence strings for a human to confirm.

| Tier | Authority                                       | Confidence | What it means                                                                                                                                                                                                   |
| ---- | ----------------------------------------------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | **Existing in-repo string for the same action** | Highest    | The corpus already ships a string for this exact semantic action (e.g. reusing the feature's own "Link fields to signatures"). Reuse its sound wording, but do not propagate a clear corpus-quality defect.     |
| 1    | **Translation-memory match**                    | High       | A near-identical already-translated string exists in the same locale. Mirror its choices.                                                                                                                       |
| 2    | **Glossary**                                    | Medium     | A canonical term from the corpus-mined glossary (or, consulted only, Microsoft Terminology, see below). Use it for generic UI verbs. See `terminology.md` for what the product's ambiguous terms actually mean. |
| 3    | **Real-world usage**                            | Advisory   | For the risky residue only: validate against how comparable software phrases this action online. Lowers/raises confidence; never a hard pass/fail.                                                              |

## Corpus-quality exceptions

Shipped wording is strong evidence, not proof that every character is correct. Do not copy an
unambiguous mechanical defect such as a misspelling, duplicated character, or malformed punctuation
into a new key merely for consistency.

- Correct the new key and tag it `corpus-quality` for human review, naming the existing anchor and
  the deliberate correction.
- Keep the existing anchor outside the feature's translation batch. Report it as follow-up debt
  rather than silently widening scope.
- Treat terminology, register, and contextual disagreements as semantic review candidates, not
  typos. The exception is for defects whose correction does not change meaning.

## When to spend a tier-3 web check

Tier 3 is expensive and its results are not reproducible, so it is **advisory only** and reserved for the two string shapes that machine translation most often gets wrong:

- **Standalone connectors and prepositions** lifted out of a sentence. A bare "to", "of", "with" glued between two interpolated nouns rarely translates as a standalone word. Particle and case-marking languages (ja, ko) attach it to the noun; others inflect. This is where literal MT produces nonsense.
- **Recomposed or templated sentences in SOV / particle / RTL languages** (ja, ko, and ar for bidi ordering): multiple placeholders interleaved with words, where word order and particles must be rebuilt, not substituted positionally.

For those, ask: how does comparable professional software localize this same action in this language? Prefer the typical, idiomatic phrasing. For everything else, tiers 0-2 are sufficient and carry no network or licensing cost.

Picking these strings is itself a model judgment, not a deterministic rule: a bare-connector predicate misses the common case (a connective embedded between placeholders) and a placeholder-count predicate over-selects ordinary sentences. Since the review pass already runs on the diff (a small changed set), choose the risky ones by reading them, not by a pre-filter.

## Geographic proper nouns have their own reference

Country, state, and province names (`esl.countries.`, `esl.us_states.`, `esl.ca_provinces.`) are not graded by these tiers. Their target form is attested, not derivable, and the corpus is the weakest authority for them, so `check-content.mjs` audits them against a bundled CLDR reference instead — see "Proper nouns and place names" in `target-language-conventions.md`. Treat that reference above the corpus for this class.

## Microsoft Terminology is consult-only

Microsoft Terminology is a useful reference for canonical UI verbs across languages, but its license (in the download, `MicrosoftTermCollection.zip`) restricts redistribution. **Never commit Microsoft-derived data into this repo or any product repo.** Consult it while reasoning; cite the corpus, not Microsoft, in what ships. The corpus is our own and carries the weight.
