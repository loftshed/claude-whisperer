// Advisory content heuristics shared by check-content.mjs. Pure data and
// predicates only (no fs), so the CLI stays thin and every rule is unit-tested
// directly. These produce REVIEW CANDIDATES, never gate failures: each has real
// false positives (a legitimately long German button, a word correctly identical
// across Latin-script locales), so a human judges, the script only surfaces.

// Latin-script locales where a single English word is often correctly identical
// in the target, so an identical-to-source match there is not evidence of a
// missed translation. A deliberate superset of the locales present in any one
// repo (extra codes are harmless); it must include every Latin-script locale
// that ships and exclude every non-Latin one, since the single-word exclusion
// (isExcludedFromIdentical) keys off it.
export const LATIN_SCRIPT_LOCALES = new Set([
  "fr",
  "de",
  "es",
  "es-AR",
  "es-ES",
  "it",
  "nl",
  "no",
  "da",
  "pl",
  "pt",
  "pt-BR",
  "ro",
  "cs",
  "hu",
  "tr",
  "sv",
  "fi",
  "sk",
  "hr",
  "sl",
  "et",
  "lv",
  "lt",
  "sq",
  "sr-Latn",
]);

// Proper-noun brand and product names that are never correctly translated, so a
// locale that altered or dropped one is a real defect. Supplied per repository
// via the `brands` field of `.i18n-catalogs.json` (see lib/adapters.mjs); this
// narrow list drives the brand-drift check. Generic acronyms and feature
// phrases (Q&A, Fast Track, SMS, API, …) are deliberately NOT configured:
// locales legitimately localize them (fr "Q&R", de "F&A"), so flagging their
// absence is noise.

// Broader set of terms that ship in English: generic technical acronyms and
// proper nouns. Used only to EXCLUDE a value from the identical-source check (a
// value that IS one of these is correctly left in English); not used for
// brand-drift, where the acronyms would be false positives. A repository's
// `keepInEnglish` config field replaces this default list.
export const KEEP_IN_ENGLISH = [
  "OAuth 2.0",
  "OAuth",
  "SAML",
  "SSO",
  "REST",
  "API",
  "SMS",
  "URL",
  "PDF",
  "Chrome",
  "Firefox",
  "Safari",
  "Edge",
];

// UI element types that do not wrap: an over-long translation truncates or
// overflows rather than reflowing, so length thresholds tighten for these.
export const TIGHT_UI_TYPES = new Set(["button", "status", "title"]);

// Key-name substrings that classify a string's UI role. First match wins. Drives
// the length threshold only, so a rough classification is enough.
export const STRING_TYPE_PATTERNS = [
  ["button", ["button", "btn", "cta"]],
  ["status", ["status", "badge"]],
  ["title", ["title", "header", "heading"]],
  ["error", ["error", "err", "invalid", "failed", "forbidden"]],
];

// Length ratio (translation chars / source chars) that trips a warning, by
// source length: short strings tolerate more expansion, long ones less.
const LENGTH_THRESHOLDS = [
  [15, 3.5],
  [40, 2.5],
  [Infinity, 2],
];

export function detectStringType(key) {
  const k = key.toLowerCase();
  for (const [type, needles] of STRING_TYPE_PATTERNS) {
    if (needles.some((n) => k.includes(n))) return type;
  }
  return "general";
}

export function normalizeWs(s) {
  return s.replaceAll(/\s+/g, " ").trim();
}

// Placeholders (both dialects), positional tokens, and HTML tags removed, so we
// can judge whether a string carries real translatable text. Only SIMPLE ICU
// placeholders (`{name}`, `{0}`) are stripped; complex nested ICU
// (`{count, plural, …}`) is left intact rather than parsed — those few strings
// carry real text and fall through harmlessly (they read as "has text").
function textOnly(s) {
  return s
    .replaceAll(/%\([^)]*\)[a-zA-Z]/g, "")
    .replaceAll(/%\{[^}]*\}/g, "")
    .replaceAll(/\{[a-zA-Z0-9_]+\}/g, "")
    .replaceAll(/<[^>]+>/g, "")
    .replaceAll(/\s+/g, " ")
    .trim();
}

function wordCount(s) {
  const t = textOnly(s);
  return t ? t.split(" ").length : 0;
}

// East-Asian wide/fullwidth glyphs (BMP): Hangul Jamo, CJK radicals/symbols,
// kana, CJK ideographs incl. Ext A, Hangul syllables, compatibility ideographs,
// and fullwidth forms. Astral CJK (Ext B+) is omitted — those historic ideographs
// don't appear in product UI strings. Expressed as a regex so the code point
// bounds live in regex escapes, not numeric literals (which the lint/format
// toolchain fights over for hex casing).
const WIDE_GLYPH =
  /[\u{1100}-\u{115F}\u{2E80}-\u{303E}\u{3041}-\u{33FF}\u{3400}-\u{4DBF}\u{4E00}-\u{9FFF}\u{AC00}-\u{D7A3}\u{F900}-\u{FAFF}\u{FF00}-\u{FF60}]/u;

// Approximate display width: wide glyphs occupy ~2 columns, so a Chinese string
// with few characters can be physically wider than its longer English source.
// Counting raw `.length` would make length-overflow structurally blind to CJK;
// this keeps the ratio meaningful across scripts.
function displayWidth(s) {
  let w = 0;
  for (const ch of s) w += WIDE_GLYPH.test(ch) ? 2 : 1;
  return w;
}

function escapeRe(s) {
  return s.replaceAll(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`);
}

// True when an identical (locale value === source value) string is legitimate,
// so it should NOT be flagged as a missed translation. `keepInEnglish` is the
// repository's configured list (defaults to KEEP_IN_ENGLISH above).
export function isExcludedFromIdentical(
  key,
  source,
  locale,
  { geoPrefixes = [], keepInEnglish = KEEP_IN_ENGLISH } = {},
) {
  const s = source.trim();
  const core = textOnly(source);
  if (
    core === "" || // only placeholders / markup / whitespace
    !/[a-zA-Z]/.test(core) // no translatable letters ("© %(year)s", "1.")
  ) {
    return true;
  }
  // url / email / bare-domain literal (tolerate a trailing " *" required-field marker).
  // Must genuinely look like one — a lone "/" ("and/or") does not qualify.
  const lit = core.replace(/[\s*]+$/, "");
  return (
    (!/\s/.test(lit) &&
      (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(lit) || // email
        /^(?:https?:\/\/|www\.)/i.test(lit) || // url
        /^[\w-]+(?:\.[\w-]+)+(?:\/\S*)?$/.test(lit))) || // bare domain, optional path
    /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/.test(s) || // snake_case identifier (needs an underscore)
    /^[a-z][a-z0-9]*[A-Z][a-zA-Z0-9]*$/.test(s) || // camelCase identifier (needs an internal capital)
    /^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+$/.test(s) || // dot.notation id
    geoPrefixes.some((p) => key.startsWith(p)) || // geographic proper nouns
    keepInEnglish.some((t) => s.toLowerCase() === t.toLowerCase()) || // pure brand term
    (LATIN_SCRIPT_LOCALES.has(locale) && wordCount(source) <= 1) // single word
  );
}

// Locale value byte-for-byte equal to the source (whitespace-normalized) is a
// string that was copied, not translated. English variants (en, en-GB) are
// exempt. Returns a candidate or null.
export function identicalCandidate(key, source, translation, locale, options = {}) {
  if (
    locale.startsWith("en") ||
    translation.trim() === "" || // empty: the gate reports it as untranslated
    isExcludedFromIdentical(key, source, locale, options)
  ) {
    return null;
  }
  if (normalizeWs(translation) === normalizeWs(source)) {
    return { code: "identical-source", key, source, translation };
  }
  return null;
}

// Translation far longer than the source may not fit a fixed UI slot. Threshold
// depends on source length and tightens for non-wrapping element types. Returns
// a candidate or null.
export function lengthCandidate(key, source, translation) {
  const sourceChars = displayWidth(source.trim());
  const trChars = displayWidth(translation.trim());
  if (sourceChars === 0 || trChars === 0) return null;

  const ratio = trChars / sourceChars;
  const base = LENGTH_THRESHOLDS.find(([max]) => sourceChars < max)[1];
  const type = detectStringType(key);
  const threshold = TIGHT_UI_TYPES.has(type) ? Math.round(base * 0.8 * 10) / 10 : base;
  if (ratio <= threshold) return null;

  return {
    code: "length-overflow",
    key,
    ratio: Math.round(ratio * 10) / 10,
    threshold,
    type,
    severity: ratio > threshold * 1.4 ? "very-long" : "long",
    srcChars: sourceChars,
    trChars,
  };
}

// Whitespace-collapsed, case-folded, and apostrophe-variant-folded, for
// comparing a value against a reference name where casing, spacing, and
// apostrophe typography carry no signal (diacritics do).
function normalizeCompare(s) {
  return normalizeWs(s).toLowerCase().replaceAll(/[’ʼ]/g, "'");
}

// Geographic proper nouns (countries, states, provinces) are a closed set with
// an authoritative per-locale reference (bundled CLDR). Unlike the generic
// identical-source heuristic, this audits the shipped value against that
// reference, so it speaks only where reference data exists. `geoTable` maps a
// key prefix to { code: { locale: name } }; the code is whatever follows the
// prefix in the key, tried as-is then upper/lower. Two candidate reasons:
//   untranslated  value equals the English source, but the locale has a real
//                 (different) translated name — a missed translation.
//   diverges      value differs from both the source and the reference name —
//                 possibly a wrong or outdated corpus entry.
// Silent when the code/locale is absent (defer, no guess), when the reference
// equals the source (the name is correctly kept in English here), for English
// locales, and when no table is supplied. Advisory candidate, never a verdict.
export function geoMismatchCandidate(key, source, translation, locale, { geoTable = {} } = {}) {
  if (locale.startsWith("en")) return null;
  const prefix = Object.keys(geoTable).find((p) => key.startsWith(p));
  if (!prefix) return null;
  const code = key.slice(prefix.length);
  const entry =
    geoTable[prefix][code] ??
    geoTable[prefix][code.toUpperCase()] ??
    geoTable[prefix][code.toLowerCase()];
  const expected = entry?.[locale];
  if (expected === undefined) return null;

  const source_ = normalizeCompare(source);
  const reference = normalizeCompare(expected);
  const value = normalizeCompare(translation);
  if (
    value === reference || // matches the reference: correct
    reference === source_ // name is not translated in this locale: keep English
  ) {
    return null;
  }

  const reason = value === source_ ? "untranslated" : "diverges";
  return { code: "geo-mismatch", reason, key, source, translation, expected, locale };
}

// Proper-noun brand names present as whole words in the source but absent from
// the translation, i.e. localized or dropped when the brand must ship in English.
// `brands` is the repository's configured list (see lib/adapters.mjs) — generic
// acronyms are excluded because locales legitimately localize them. Returns a
// candidate or null.
export function brandDriftCandidate(key, source, translation, brands = []) {
  const matched = brands.filter((term) => {
    const word = new RegExp(String.raw`\b${escapeRe(term)}\b`, "i");
    return word.test(source) && !word.test(translation);
  });
  // Drop a term subsumed by a longer matched one ("OneSpan" when "OneSpan Sign"
  // also matched) so a single dropped brand is reported once.
  const terms = matched.filter((t) =>
    matched.every((o) => o === t || !o.toLowerCase().includes(t.toLowerCase())),
  );
  return terms.length > 0 ? { code: "brand-drift", key, terms } : null;
}
