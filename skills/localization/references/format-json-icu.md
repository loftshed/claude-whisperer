# JSON/ICU catalog format

Read this reference when the detected catalog uses `format: "json"` with `syntax: "icu"` (React Intl).

## Source and usage

- Keep stable semantic IDs and English `defaultMessage` values in colocated `messages.js` files. Default-export `defineMessages` with meaningful camelCase local names; those names can differ from the final segment of the ID. With `messageIndirection` enabled, the skill's discovery scripts resolve those descriptors to their catalog IDs.
- Keep every descriptor's English default synchronized with the source JSON entry. Discovery reads the source file, so a descriptor-only change needs source validation even when discovery returns no entries.
- Preserve IDs referenced by backend errors or external overrides. Values that arrive with named printf arguments and are converted at an API boundary are not catalog strings.

## Translation contract

Preserve [ICU argument names and types, selectors, and rich-text tags](https://formatjs.github.io/docs/core-concepts/icu-syntax/).
Keep complete sentences in one message. Preserve exact-number branches such as
`=0` and `=1`; adapt grammatical plural categories to the target language and
include `other` in every plural/select. Keep rich-text callbacks, destinations,
and handlers consistent with the source. A translation may add exact-number branches to an existing
plural argument when its wording requires them. At an existing sanitized HTML boundary, preserve the
message's ICU quoting of literal markup.

For example, this descriptor uses a local name different from its catalog ID:

```js
export default defineMessages({
  uploadedCount: {
    id: "app.file_upload.uploaded_count",
    defaultMessage:
      "{count, plural, =0 {No files uploaded} one {# file uploaded} other {# files uploaded}}",
    description: "Number of files uploaded successfully.",
  },
});
```

Translate `app.file_upload.uploaded_count` as one ICU string in each locale.
`formatMessage(messages.uploadedCount, { count })` consumes that ID; plural
branches remain inside the value.

The runtime preserves intentionally empty translations. Investigate blank-value
discovery candidates against their descriptors and consumers before replacing
them. Use the skill's exclusion flow when the user chooses to retain an empty
value. English fallback does not establish translated coverage.

## Validation

If the repository documents its own catalog validators (ICU parsing, descriptor/ID
consistency), run them alongside the skill's structural gate. Verify those paths
in the current checkout and report missing or failing checks. A descriptor-only ID
or a default that differs from the source file is a source defect to resolve
before translation.
