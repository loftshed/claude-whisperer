# Source clarity: does the English read like a product, not a stack trace?

Criterion 1, the highest priority. Runs on the **English source only**, so there is no cross-language risk here. The question: would a non-developer end user understand this string? Developer jargon that leaked into user-facing copy is the defect.

## The context rule

A string may be technical **only if it is technical by nature**: a `console.*` message, a developer-facing log line, an `error_detail` meant for support, a stack trace. Judge by the key path. These key shapes are allowed to stay technical:

- contains `console`, `log`, `debug`, `error_detail`, `errorDetail`, `stackTrace`, `dev`

Everything a sender or signer actually reads in the UI is not: button labels, dialog text, field labels, `aria_*` / accessibility strings, titles, descriptions, toasts, validation messages. Hold those to plain language.

## Do not flag legitimate product terms

A vocabulary blocklist is the wrong tool here. OneSpan is developer-adjacent, so the technical-sounding words that appear in shipped copy are correct usage: `session`, `API`, `token`, `callback`, and `JSON` are all legitimate. The genuinely-technical words (`null`, `undefined`, `NaN`, `boolean`, `exception`, `string`, `array`, `render`, `promise`, `payload`, `enum`) do not appear in user-facing copy at all. A keyword scan would therefore be almost entirely false positives. Do **not** flag `session`, `token`, `API`, `callback`, `JSON`, `object`, `render`, `promise`, or `string`; in this product they are the right words.

## The deterministic tripwire (near-zero false positives)

Only patterns that are never intentional in shown copy. These are the sole part safe to treat as a hard signal:

- standalone runtime literals as whole words: `null`, `undefined`, `NaN`, `boolean` (zero today, so this is a pure regression guard)
- an HTTP status code as the entire message: `"403"`, `"500"`
- a `snake_case` or `camelCase` identifier sitting in the visible text, outside `%(...)` / `{...}` placeholders and outside tags
- a stack-trace or dev-imperative shape: "failed to parse", "unexpected error in handler", "at line N"
- a leaked or unrendered tag in a context that should not have one

Everything past this tripwire is model judgment, below.

## Beyond jargon: is the _word_ right for a non-developer?

The harder, model-judgment half. A string can be jargon-free and still wrong for the audience: too literal, too clever, or a term the feature doesn't actually use elsewhere. The canonical example from this codebase: an action was labeled "Map" (developer's mental model) when the feature's own copy, and what a sender understands, is "Link". When a verb feels like the developer's word rather than the user's, flag it and point at how the rest of the app names the same action (tier 0 of the authority stack).

A bare, ambiguous abbreviation in shown copy is a source problem too: "CA" for California or Canada, standing alone with no context, cannot be translated reliably. Flag it for disambiguation at the source rather than guessing. (Keyed country and state catalogs already disambiguate by namespace; this is about free-text UI strings.)
