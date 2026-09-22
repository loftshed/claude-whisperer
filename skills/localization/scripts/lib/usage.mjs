// Pure helpers for find-usages.mjs: locate where a translation key is consumed
// in the repo's source code, so element type (button, heading, toast, …) can be
// judged from the real render site instead of guessed from the key path. The
// grep itself runs in the CLI; everything here is deterministic parsing and
// ranking over its output.

// Characters that can extend a dotted key into a longer identifier. A hit for
// `a.b.confirm` inside `a.b.confirm.title` (or `pre_a.b.confirm`) is a different
// key, not a usage.
const KEY_EXTENSION = /[\w.-]/;

// What can follow `<key>.` and still mean "this key, with its last segment built
// at runtime": a template interpolation, or the closing quote of a literal that
// is about to be concatenated.
const DYNAMIC_SUFFIX = /^\.(?:\$\{|['"`])/;

// True when `text` composes `key`'s final segment at runtime, as
// `` `<key>.${variant}` `` or `'<key>.' + variant`. The plain token match
// deliberately refuses these, since a trailing `.` normally means a longer key.
// For a key already known to be a plural (its node holds a complete `one`/`other`
// pair) the longer key is precisely one of its own forms, so the hit is a real
// reference to the family rather than to something else.
export function keyOccursAsDynamicPrefix(text, key) {
  let from = 0;
  while (true) {
    const at = text.indexOf(key, from);
    if (at === -1) return false;
    const before = at === 0 ? "" : text[at - 1];
    if (!KEY_EXTENSION.test(before) && DYNAMIC_SUFFIX.test(text.slice(at + key.length))) {
      return true;
    }
    from = at + 1;
  }
}

// True when `key` occurs in `text` as a whole token — not embedded in a longer
// dotted key or identifier.
export function keyOccursAsToken(text, key) {
  let from = 0;
  while (true) {
    const at = text.indexOf(key, from);
    if (at === -1) return false;
    const before = at === 0 ? "" : text[at - 1];
    const after = text[at + key.length] ?? "";
    if (!KEY_EXTENSION.test(before) && !KEY_EXTENSION.test(after)) return true;
    from = at + 1;
  }
}

// Parse `git grep -n` output into { file, line, text } hits. Tolerates blank
// trailing lines; a malformed line is skipped rather than guessed at.
export function parseGitGrep(raw) {
  const hits = [];
  const lines = (raw || "").split("\n");
  for (const line of lines) {
    const m = /^(.*?):(\d+):(.*)$/.exec(line);
    if (m) hits.push({ file: m[1], line: Number(m[2]), text: m[3] });
  }
  return hits;
}

// Test/mock files still prove a key is wired up, but they don't show the render
// site — rank them after production hits.
export function isTestPath(file) {
  return /(^|\/)(__tests__|__mocks__|tests?)\/|[._-](test|spec)\.[a-z]+$/i.test(file);
}

// True when `text` references the message by its local property as a member access
// (`messages.alias`, `intl.formatMessage(messages.alias)`), not as part of a longer
// identifier. This is the defineMessages render form: the render site names
// the property, while the catalog carries the full dotted key. `(?![\w$])` stops `alias`
// from matching inside a longer property name.
export function aliasUsedAsMember(text, alias) {
  const escaped = alias.replaceAll(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`);
  return new RegExp(String.raw`\.${escaped}(?![\w$])`).test(text);
}

// Keep exact-token hits, order production code before tests, and cap the list.
// Returns { shown, omitted } so the caller can report what was dropped.
//
// With `alias` (a message's local property, for defineMessages-style catalogs), a hit
// also counts when it references the property as a member access — so the actual render
// site is found, not just the `messages.js` definition line, which the literal dotted key
// only ever matches in that convention. Real render sites rank above the definition (the
// hit carrying the full dotted key), and each returned hit is tagged `definition`.
export function rankUsages(hits, key, { cap = 5, alias = null, dynamicPrefix = false } = {}) {
  const matches = (text) =>
    keyOccursAsToken(text, key) || (dynamicPrefix && keyOccursAsDynamicPrefix(text, key));
  if (!alias) {
    const exact = hits.filter((h) => matches(h.text));
    const production = exact.filter((h) => !isTestPath(h.file));
    const tests = exact.filter((h) => isTestPath(h.file));
    const ordered = [...production, ...tests];
    return { shown: ordered.slice(0, cap), omitted: Math.max(0, ordered.length - cap) };
  }
  const seen = new Set();
  const matched = [];
  for (const hit of hits) {
    if (!matches(hit.text) && !aliasUsedAsMember(hit.text, alias)) continue;
    const id = `${hit.file}:${hit.line}`;
    if (seen.has(id)) continue;
    seen.add(id);
    matched.push({ ...hit, definition: hit.text.includes(key) });
  }
  // render+prod (0) < render+test (1) < definition+prod (2) < definition+test (3): any real
  // render site outranks the definition, and production outranks tests within each group.
  const rank = (hit) => (hit.definition ? 2 : 0) + (isTestPath(hit.file) ? 1 : 0);
  const ordered = [...matched].sort((a, b) => rank(a) - rank(b));
  return { shown: ordered.slice(0, cap), omitted: Math.max(0, ordered.length - cap) };
}
