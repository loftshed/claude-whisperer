# Mailbox MCP audit

The mailbox supports published MCP `2026-07-28` and legacy initialization clients. It runs locally
through stdio and exposes seven tools. This audit applies to the checked-out implementation; already
running server processes need a restart to load changes.

## Protocol sources

Checked on 2026-10-06 against the official [current revision](https://modelcontextprotocol.io/specification/latest),
which resolves to `2026-07-28`, its [changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog),
[versioning rules](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning),
[stdio binding](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio),
[tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools), and
[caching rules](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching).
The unpublished draft is not advertised as a supported version.

## Guidance and evidence

| Requirement                  | Implementation and evidence                                                                                                                                                                                                                                                                                                                                                                       |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Current protocol             | Modern calls need version/capabilities metadata; `server/discover` advertises revisions. Ordinary results carry `resultType: complete` and server identity. Unsupported versions return `-32022` with supported/requested versions. Contract tests use real stdio without initialization.                                                                                                         |
| Legacy interoperability      | `initialize`, initialized notifications, ping and prior protocol versions remain supported. A modern request cannot inherit client identity from a previous request or handshake.                                                                                                                                                                                                                 |
| Predictable discovery        | Seven tools in stable name order. Modern discovery and catalogs carry `ttlMs: 3600000` and `cacheScope: public`, containing no peer data. Unsupported list cursors return an actionable error.                                                                                                                                                                                                    |
| Discoverable operations      | Preserve installed short names under the host's server namespace; adding duplicate prefixed aliases would enlarge discovery and invalidate tool-specific permissions. Server identity is `peer_mailbox_mcp`. Interactive coordination stays separate from the existing run-worker CLI workflow.                                                                                                   |
| Validated inputs             | Closed schemas reject unknown fields, wrong types, enums, lengths, number ranges, array bounds, invalid ids, and oversized UTF-8 messages before registering or sending. Offset reads cannot mark a shrinking unread list.                                                                                                                                                                        |
| Typed outputs                | All seven tools declare output schemas and return `structuredContent`. Nine captured modern responses pass the official `2026-07-28` JSON Schema through AJV 2020-12, including all seven output schemas.                                                                                                                                                                                         |
| Honest annotations           | All four hints are present. Inbox/wait can consume unread mail; send can wake another agent. Annotations do not authorize access or replace host permissions.                                                                                                                                                                                                                                     |
| Bounded context              | Default pages contain at most 20 items, configurable up to 50. Mail batches use a soft 12 KiB body budget. An oversized first body remains intact; only returned messages can be consumed. Peer pages expose count/continuation and unread pages expose continuation. Tests drain pages and verify every body remains retrievable.                                                                |
| Local I/O and cancellation   | Waits run on cancellable worker threads; other requests continue. Eight pending waits maximum, finished threads discarded, duplicate pending ids refused, EOF cancels and joins workers. Inbox bodies stream from disk. Existing cancellation, concurrent-reader, hook and run tests pass.                                                                                                        |
| Useful errors                | Correctable tool failures use `isError`. Routing errors provide current-peer/native-channel guidance. Storage and unexpected errors omit exception contents and private paths. Malformed JSON, ids, methods and metadata return protocol errors.                                                                                                                                                  |
| Project and authority limits | Existing project filters, explicit cross-project scope, native-first communication, escaped peer wrappers and teammate authority notices remain. Regression tests cover scope, message delivery, feedback and wakes.                                                                                                                                                                              |
| SDK and transport choice     | Retain the repository's standard-library-only Python runtime for copied plugin installs. The validator covers only the closed subset emitted by this server, not arbitrary client-provided schemas. HTTP auth, DNS controls, resources, prompts, subscriptions, sampling and remote schema resolution are unused. Local peer identity is not an authentication boundary against the same OS user. |
| Inspector interoperability   | Official Inspector 2.9.0 accepts legacy and modern catalog/call checks. Its strict portability check reports no errors or warnings after nullable schemas use `anyOf`. Installed only in a temporary tooling directory; runtime dependencies are unchanged. Latest npm tag and official license checked; temporary install audit reports zero known vulnerabilities.                              |
| Agent usability              | Ten independent questions are solved using only peer discovery and read-only inbox pages. The deterministic answer-key check and an independent agent both return all ten expected values. The agent used two peer pages and six inbox pages with no tool failures.                                                                                                                               |

## Context cost

Compared with commit `1201ec4`, server instructions shrink from 2,401 to 786 UTF-8 bytes, about 67%.
The compact full tool catalog grows from 4,850 to 8,487 bytes because it now includes validated paging,
caller identity, annotations and output schemas. Cold discovery therefore grows from 7,251 to 9,273
bytes. These are byte measurements, not tokenizer counts; hosts differ in which fields they place in
model context and how they cache them. Do not claim a universal token reduction from these numbers.

The repeated cost is bounded separately. Markdown carries message bodies once, puts routing metadata
in structured content, and retains the authority notice. JSON deliberately duplicates full structured
data in text for client compatibility and is opt-in. Focus returns a short confirmation. Hooks and CLI
mail delivery no longer append the full project/native-channel rule to every batch. Modern clients can
cache the stable discovery/catalog; a TTL expiry does not start a background poll.

Wait polling happens inside the server and makes no model calls. Hosts without delivery hooks should
read once at a turn boundary and wait for a needed reply. Empty results and timeouts do not request
automatic retries. Quiet sends avoid activation. Waking an idle agent can still use that agent's quota.
JSON batches include the peer-authority notice. No server-side claim can prove that a particular host caches metadata or that an agent will never poll.

## Reproduce verification

The full plugin suite passed 370 tests. On macOS, the unchanged installer test needs a canonical
temporary directory: `/var` resolves to `/private/var`, so its literal-path comparison fails under the
usual alias. `TMPDIR=/private/tmp` avoids that existing failure. Existing subprocess tests also emit
file-handle ResourceWarnings while passing.

From the repository root:

```sh
python3 -B -m unittest discover -s tests/plugins/orchestrate/skills/orchestrate -p 'test_mailbox*.py'
python3 -B -m unittest discover -s tests/plugins/orchestrate/skills/orchestrate -p 'test_*.py'
```

The [evaluation XML](../tests/plugins/orchestrate/skills/orchestrate/evaluations/evaluation.xml)
contains ten stable questions and answer keys. Its [synthetic snapshot](../tests/plugins/orchestrate/skills/orchestrate/evaluations/snapshot.json)
is served in a temporary Git project:

```sh
python3 -B tests/plugins/orchestrate/skills/orchestrate/mailbox_evaluation_server.py
```

Use current-spec discovery and tool calls with `session: "investigator"`. Only `peers` and
`inbox(markRead:false)` are accepted. Page with `limit: 2`, `offset`, and `response_format: "json"`.
The test `test_frozen_evaluation_answers_are_retrievable_without_consuming_mail` verifies the keys
through actual stdio calls and confirms all messages stay unread. For an independent agent evaluation,
provide the questions without answer keys or implementation access.

The frozen evaluation proves this synthetic workflow, not all models, host token counts or live idle
activation. The existing wake suite covers adapter behavior separately; this audit never contacted a
live peer or changed a harness registration.
