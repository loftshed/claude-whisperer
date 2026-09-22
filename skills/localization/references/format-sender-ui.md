# Sender UI localization format

Read the checkout's scoped instructions and `core-i18n` skill before changing copy.
All application surfaces, including `src/app/new`, use React Intl through the
shared `AppIntlProvider` and one catalog:

| Catalog        | Location                       | Format                            |
| -------------- | ------------------------------ | --------------------------------- |
| `app-messages` | `src/shared/i18n/translations` | Flat message IDs with ICU strings |

The detector selects this catalog by default. Use `app-messages` in the batch
manifest and read the shipped locale files to establish the target set.

## Source and usage

- Keep stable semantic IDs and English `defaultMessage` values in colocated `messages.js` files. Default-export `defineMessages` with meaningful camelCase local names; those names can differ from the final segment of the ID.
- Keep every descriptor's English default synchronized with its flat `en.json` entry. The bundled discovery scripts read `en.json`, so a descriptor-only change needs source validation even when discovery returns no entries.
- In React, destructure `formatMessage` from `useIntl()` and pass native descriptors and values. Outside React, obtain `getIntl()` from `@shared/i18n/intlStore` at formatting time. Follow the repository-wide restriction on `FormattedMessage`.
- Preserve IDs referenced by backend errors and package overrides. Account overrides arrive with named printf arguments and are converted at the API boundary; application catalogs contain ICU strings.

## Translation contract

Preserve [ICU argument names and types, selectors, and rich-text tags](https://formatjs.github.io/docs/core-concepts/icu-syntax/).
Keep complete sentences in one message. Preserve exact-number branches such as
`=0` and `=1`; adapt grammatical plural categories to the target language and
include `other` in every plural/select. Keep rich-text callbacks, destinations,
and handlers in React. A translation may add exact-number branches to an existing
plural argument when its wording requires them. At an existing sanitized HTML boundary, preserve the
message's ICU quoting of literal markup.

For example, this descriptor uses a local name different from its catalog ID:

```js
export default defineMessages({
  uploadedCount: {
    id: "esl.file_upload.uploaded_count",
    defaultMessage:
      "{count, plural, =0 {No files uploaded} one {# file uploaded} other {# files uploaded}}",
    description: "Number of files uploaded successfully.",
  },
});
```

Translate `esl.file_upload.uploaded_count` as one ICU string in each locale.
`formatMessage(messages.uploadedCount, { count })` consumes that ID; plural
branches remain inside the value.

The runtime preserves intentionally empty translations. Investigate blank-value
discovery candidates against their descriptors and consumers before replacing
them. Use the skill's exclusion flow when the user chooses to retain an empty
value. English fallback does not establish translated coverage.

## Validation

Run these consumer-owned checks alongside the skill's structural gate:

```bash
node internals/scripts/validate-intl.mjs
node internals/scripts/lint-translation-keys.mjs
```

The first parses shipped ICU messages and checks argument contracts and required
exact-number selectors. The second checks descriptor IDs, English defaults, and
colocated references across application source. Verify these paths in the current
checkout and report missing or failing checks. A descriptor-only ID or a default
that differs from `en.json` is a source defect to resolve before translation.
