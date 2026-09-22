# ESL backend localization format

Read this reference only when the detected repository is esl.backend.

Notification catalogs are Java properties using `MessageFormat`:

- Preserve every numeric argument and its format type.
- When a value contains arguments, write literal apostrophes as `''`. A single apostrophe begins a quoted run and can turn later `{n}` arguments into literal text.
- In values without arguments, keep ordinary single apostrophes. Those values are returned unformatted, so `''` would render as two apostrophes.
- Preserve properties escaping and logical continuation semantics.
- The writer appends new entries as ASCII-safe property lines while preserving every untouched byte.
- The structural gate detects malformed Unicode escapes, duplicate keys, argument/type loss, braces, choices, and apostrophe-quoting errors.

For custom number, date, or time patterns, validate the proposed messages with the consumer's JVM formatter before approval. The bundled scanner checks argument types and structural syntax; Java also validates each formatter's pattern language.
