// Extract a comparable "signature" of the interpolation and markup in a
// translation string, so a locale can be checked for parity against the source.
//
// Four placeholder dialects, one interface:
//   sprintf (Counterpart)     "%(name)s", "%(count)d", positional "%s"
//   ICU (React Intl)          "{name}", "{count, plural, one {#} other {#}}"
//   Polyglot (React Admin)    "%{name}", "one |||| many"
//   MessageFormat (Java)      "{0}", "{1,number}"
//
// Built-ins only: plugin scripts run from the marketplace cache dir and cannot
// resolve the target repo's node_modules (sprintf-js, @formatjs/...), so the
// parsers are hand-rolled rather than delegated to those libraries.

// Matches the default Array#sort() ordering for strings (compares by UTF-16
// code unit) so passing an explicit comparator doesn't change emitted order.
function compareOrdinal(a, b) {
  if (a === b) return 0;
  return a > b ? 1 : -1;
}

// ── sprintf ──────────────────────────────────────────────────────────────────

const SPRINTF_ARGUMENT =
  /^%(?:([1-9]\d*)\$|\(([^)]+)\))?(?:\+)?(?:0|'[^$])?(?:-)?(?:\d+)?(?:\.\d+)?([b-gijosuxX])/;
const SPRINTF_KEY = /^[A-Za-z_]\w*(?:(?:\.[A-Za-z_]\w*)|\[\d+\])*$/;

// The format letter is part of each part so swapping %(foo)s for %(foo)d is a
// mismatch: sprintf accepts both, but the wrong letter throws at runtime when
// the value's type doesn't match.
function sprintfSignature(s) {
  const parts = [];
  let index = 0;
  let position = 1;
  let argumentKind = null;
  let error = null;
  while (index < s.length) {
    index = s.indexOf("%", index);
    if (index < 0) break;
    if (s[index + 1] === "%") {
      index += 2;
      continue;
    }
    const match = SPRINTF_ARGUMENT.exec(s.slice(index));
    if (!match || (match[2] && !SPRINTF_KEY.test(match[2]))) {
      error = "malformed or unescaped '%' (use '%%' for a literal percent)";
      break;
    }
    const [, explicitPosition, name, letter] = match;
    const kind = name ? "named" : "positional";
    if (argumentKind && argumentKind !== kind) {
      error = "mixing named and positional sprintf placeholders is not supported";
      break;
    }
    argumentKind = kind;
    // Explicit references can reorder arguments; implicit references consume
    // the next argument, even after an explicit reference.
    parts.push(name ? `${name}:${letter}` : `<pos:${explicitPosition ?? position++}:${letter}>`);
    index += match[0].length;
  }
  return { parts: parts.sort(compareOrdinal), error };
}

// ── Polyglot ────────────────────────────────────────────────────────────────

// Scans one Polyglot branch for "%{name}" placeholders, pushing each found
// name into `parts`. Returns an error message if the branch has an
// unterminated placeholder, otherwise null.
function scanPolyglotBranch(branch, branchIndex, isPlural, parts) {
  let index = 0;
  while (index < branch.length) {
    const open = branch.indexOf("%{", index);
    if (open === -1) return null;
    const close = branch.indexOf("}", open + 2);
    if (close === -1) {
      return "unterminated Polyglot placeholder";
    }
    const name = branch.slice(open + 2, close);
    parts.push(isPlural ? `${branchIndex}:${name}` : name);
    index = close + 1;
  }
  return null;
}

function polyglotSignature(s) {
  const parts = [];
  let error = null;
  const branches = s.split("||||");
  const isPlural = branches.length > 1;

  if (isPlural) {
    parts.push(`<plural:${branches.length}>`);
    if (branches.some((branch) => branch.trim() === "")) {
      error = "Polyglot plural forms must not be empty";
    }
  }

  for (const [branchIndex, branch] of branches.entries()) {
    const branchError = scanPolyglotBranch(branch, branchIndex, isPlural, parts);
    error ??= branchError;
  }

  return { parts: parts.sort(compareOrdinal), error };
}

// ── ICU ──────────────────────────────────────────────────────────────────────

// Single-pass scanner: collects every argument reference (including those
// nested inside plural/select submessages) and validates brace balance plus the
// ICU requirement that plural/select carry an "other" category. Honors ICU
// apostrophe escaping so quoted braces don't corrupt the scan.
const ICU_TYPES = new Set(["number", "date", "time", "plural", "select", "selectordinal"]);
const ICU_BRANCH_TYPES = new Set(["plural", "select", "selectordinal"]);
const ICU_IDENTIFIER_END = /[\p{White_Space}\p{Pattern_Syntax}]/u;
const ICU_TAG_NAME =
  /^[A-Za-z][-.\d_A-Za-z\u{B7}\u{C0}-\u{D6}\u{D8}-\u{F6}\u{F8}-\u{37D}\u{37F}-\u{1FFF}\u{200C}-\u{200D}\u{203F}-\u{2040}\u{2070}-\u{218F}\u{2C00}-\u{2FEF}\u{3001}-\u{D7FF}\u{F900}-\u{FDCF}\u{FDF0}-\u{FFFD}\u{10000}-\u{EFFFF}]*/u;

function scanIcu(s) {
  const arguments_ = [];
  const tags = new Set();
  const topLevelTags = [];
  let error = null;
  let index = 0;
  const n = s.length;

  function skipWs() {
    while (index < n && /\p{White_Space}/u.test(s[index])) index++;
  }

  // A syntax character opens a quoted run; # is syntax only inside plurals.
  // Doubled apostrophes stay inside the current run as literal apostrophes.
  function skipQuote(parentType = null, isFormatStyle = false) {
    if (s[index + 1] === "'") {
      index += 2;
      return;
    }
    const isQuoteStart =
      isFormatStyle ||
      ["{", "}", "<", ">"].includes(s[index + 1]) ||
      (s[index + 1] === "#" && ["plural", "selectordinal"].includes(parentType));
    if (!isQuoteStart) {
      index++;
      return;
    }
    index += 2;
    while (index < n) {
      const character = s[index];
      index++;
      if (character !== "'") continue;
      if (s[index] !== "'") return;
      index++;
    }
  }

  function fail(message) {
    if (!error) error = message;
  }

  function parseTag(stack) {
    const isClosing = s[index + 1] === "/";
    if (!isClosing && !/[A-Za-z]/.test(s[index + 1] ?? "")) return false;
    index += isClosing ? 2 : 1;
    const name = ICU_TAG_NAME.exec(s.slice(index))?.[0];
    if (!name) {
      fail("invalid ICU rich-text tag");
      return true;
    }
    index += name.length;
    skipWs();
    if (!isClosing && s.startsWith("/>", index)) {
      index += 2;
      tags.add(name);
      return true;
    }
    if (s[index] !== ">") {
      fail(`invalid ICU rich-text tag '${name}'`);
      return true;
    }
    index++;
    if (!isClosing) {
      stack.push(name);
      tags.add(name);
    } else if (stack.at(-1) === name) {
      stack.pop();
    } else {
      fail(`unexpected or mismatched ICU closing tag '${name}'`);
    }
    return true;
  }

  function parseArgumentBody() {
    skipWs();
    let start = index;
    while (index < n && !ICU_IDENTIFIER_END.test(s[index])) index++;
    const name = s.slice(start, index);
    skipWs();
    if (!name) {
      fail("empty ICU argument name");
      return;
    }
    // After the name, valid ICU allows only ',' (a type/category follows) or
    // '}' (end). Anything else means a bad name character, most often a
    // translator localizing the placeholder name itself, which breaks
    // interpolation because the runtime can no longer match the argument.
    if (index < n && s[index] !== "," && s[index] !== "}") {
      fail("invalid character in ICU argument name");
      while (index < n && s[index] !== "}") index++;
      if (s[index] === "}") index++;
      return;
    }

    let type = null;
    if (s[index] === ",") {
      index++;
      skipWs();
      start = index;
      while (index < n && /[A-Za-z]/.test(s[index])) index++;
      type = s.slice(start, index);
      skipWs();
      if (!type || !ICU_TYPES.has(type)) {
        fail(type ? `unknown ICU argument type '${type}'` : "empty ICU argument type");
        return;
      }
    }
    const argument = type ? { name, type } : { name };
    arguments_.push(argument);

    if (ICU_BRANCH_TYPES.has(type)) {
      if (s[index] !== ",") {
        fail(`ICU ${type} '${name}' requires categories including 'other'`);
        return;
      }
      index++;
      parseCategories(argument);
    } else if (s[index] === ",") {
      index++;
      skipWs();
      if (index === n || s[index] === "}") fail("empty ICU argument style");
      else skipFormatStyle();
    }

    if (s[index] === "}") {
      index++;
    } else {
      fail("unbalanced braces in ICU argument");
    }
  }

  function parseCategories(argument) {
    const { name, type } = argument;
    const selectors = new Set();
    argument.selectors = [];
    skipWs();
    if (
      type !== "select" &&
      s.startsWith("offset", index) &&
      (index + 6 === n || ICU_IDENTIFIER_END.test(s[index + 6]))
    ) {
      if (s[index + 6] !== ":") {
        fail("invalid ICU plural offset");
        return;
      }
      index += "offset:".length;
      skipWs();
      const offset = /^[+-]?\d+/.exec(s.slice(index));
      if (!offset || !Number.isSafeInteger(Number(offset[0]))) {
        fail("invalid ICU plural offset");
        return;
      }
      argument.offset = Number(offset[0]);
      index += offset[0].length;
    }
    while (index < n && s[index] !== "}" && !error) {
      skipWs();
      if (s[index] === "}") break;
      const start = index;
      if (type !== "select" && s[index] === "=") {
        index++;
        const number = /^[+-]?\d+/.exec(s.slice(index));
        if (!number || !Number.isSafeInteger(Number(number[0]))) {
          fail("invalid ICU exact plural selector");
          return;
        }
        index += number[0].length;
      } else {
        while (index < n && !ICU_IDENTIFIER_END.test(s[index])) index++;
      }
      const cat = s.slice(start, index);
      if (!cat) {
        fail("malformed ICU category");
        return;
      }
      if (selectors.has(cat)) {
        fail(`duplicate ICU ${type} category '${cat}'`);
        return;
      }
      selectors.add(cat);
      // Named plural categories vary by language; select values and exact
      // numeric branches are application-controlled and must survive translation.
      if (type === "select" || cat.startsWith("=")) argument.selectors.push(cat);
      skipWs();
      if (s[index] !== "{") {
        fail("malformed ICU category");
        break;
      }
      index++;
      parseSubmessage(type);
      skipWs();
    }
    if (!selectors.has("other")) fail(`ICU ${type} '${name}' missing 'other' category`);
  }

  function parseSubmessage(parentType) {
    // A tag may wrap a whole argument, but cannot open in one alternative
    // and close in another or close a tag belonging to the outer message.
    const openTags = [];
    while (index < n && !error) {
      const ch = s[index];
      if (ch === "'") {
        skipQuote(parentType);
        continue;
      }
      if (ch === "{") {
        index++;
        parseArgumentBody();
        continue;
      }
      if (ch === "<" && parseTag(openTags)) continue;
      if (ch === "}") {
        if (openTags.length > 0) fail(`unclosed ICU rich-text tag '${openTags.at(-1)}'`);
        index++;
        return;
      }
      index++;
    }
    fail("unbalanced braces in ICU submessage");
  }

  function skipFormatStyle() {
    let depth = 0;
    while (index < n) {
      const ch = s[index];
      if (ch === "'") {
        skipQuote(null, true);
        continue;
      }
      if (ch === "{") {
        depth++;
      } else if (ch === "}") {
        if (depth === 0) return;
        depth--;
      }
      index++;
    }
  }

  while (index < n) {
    const ch = s[index];
    if (ch === "'") {
      skipQuote();
      continue;
    }
    if (ch === "{") {
      index++;
      parseArgumentBody();
      continue;
    }
    if (ch === "<" && parseTag(topLevelTags)) continue;
    if (ch === "}") fail("unexpected '}'");
    index++;
  }

  if (topLevelTags.length > 0) fail(`unclosed ICU rich-text tag '${topLevelTags.at(-1)}'`);
  return { args: arguments_, tags, error };
}

function icuSignatureFromScan({ args, error }) {
  const parts = args.flatMap((argument) => {
    const key = argument.type ? `${argument.name}:${argument.type}` : argument.name;
    return [
      key,
      ...(argument.selectors ?? []).map((selector) => `${key}:${selector}`),
      ...(argument.offset ? [`${key}:offset:${argument.offset}`] : []),
    ];
  });
  return { parts: parts.sort(compareOrdinal), error };
}

function icuSignature(s) {
  return icuSignatureFromScan(scanIcu(s));
}

// ── Java MessageFormat ──────────────────────────────────────────────────────

const MESSAGE_FORMAT_TYPES = new Set(["number", "date", "time", "choice"]);
const CHOICE_LIMIT = /^[-+]?(?:Infinity|∞|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)$/;

function choiceLimitValue(raw) {
  if (["∞", "+∞", "Infinity", "+Infinity"].includes(raw)) {
    return Infinity;
  }
  if (raw === "-∞" || raw === "-Infinity") return -Infinity;
  return Number(raw);
}

function messageFormatSignature(s) {
  const parts = [];
  let error = null;

  const fail = (message) => {
    if (!error) error = message;
  };

  // Java MessageFormat never rejects an unpaired apostrophe: it quotes to the
  // end of the pattern. Any {n} swallowed by the run is then missing from the
  // signature, so the hazard surfaces as a placeholder mismatch against the
  // source rather than as a parse error on ordinary prose apostrophes.
  function skipQuote(text, start) {
    if (text[start + 1] === "'") return start + 2;

    let index = start + 1;
    while (index < text.length) {
      if (text[index] !== "'") {
        index += 1;
        continue;
      }
      if (text[index + 1] === "'") {
        index += 2;
        continue;
      }
      return index + 1;
    }
    return text.length;
  }

  function matchingBrace(text, start) {
    let depth = 0;
    let index = start;
    while (index < text.length) {
      const ch = text[index];
      if (ch === "'") {
        index = skipQuote(text, index);
        continue;
      }
      if (ch === "{") {
        depth += 1;
      } else if (ch === "}") {
        if (depth === 0) return index;
        depth -= 1;
      }
      index += 1;
    }
    if (!error) fail("unbalanced braces in Java MessageFormat argument");
    return -1;
  }

  function splitArgument(body) {
    const segments = [];
    let start = 0;
    let depth = 0;
    let index = 0;
    while (index < body.length) {
      const ch = body[index];
      if (ch === "'") {
        index = skipQuote(body, index);
        continue;
      }
      if (ch === "{") {
        depth += 1;
      } else if (ch === "}") {
        depth -= 1;
      } else if (ch === "," && depth === 0 && segments.length < 2) {
        segments.push(body.slice(start, index));
        start = index + 1;
      }
      index += 1;
    }
    segments.push(body.slice(start));
    return segments;
  }

  function choiceSegments(style) {
    const segments = [];
    let start = 0;
    let depth = 0;
    let index = 0;
    while (index < style.length) {
      const ch = style[index];
      if (ch === "'") {
        index = skipQuote(style, index);
        continue;
      }
      if (ch === "{") {
        depth += 1;
      } else if (ch === "}") {
        if (depth === 0) fail("unexpected '}' in Java MessageFormat choice style");
        else depth -= 1;
      } else if (ch === "|" && depth === 0) {
        segments.push(style.slice(start, index));
        start = index + 1;
      }
      index += 1;
    }
    if (depth !== 0) fail("unbalanced braces in Java MessageFormat choice style");
    segments.push(style.slice(start));
    return segments;
  }

  function choiceOperator(segment) {
    let depth = 0;
    let index = 0;
    while (index < segment.length) {
      const ch = segment[index];
      if (ch === "'") {
        index = skipQuote(segment, index);
        continue;
      }
      if (ch === "{") depth += 1;
      else if (ch === "}") depth -= 1;
      else if (depth === 0 && ["#", "<", "≤"].includes(ch)) return index;
      index += 1;
    }
    return -1;
  }

  function decodeChoiceText(text) {
    let output = "";
    for (let index = 0; index < text.length; index += 1) {
      if (text[index] !== "'") {
        output += text[index];
      } else if (text[index + 1] === "'") {
        output += "'";
        index += 1;
      }
    }
    return output;
  }

  function parseChoice(style) {
    let previous = null;
    for (const segment of choiceSegments(style)) {
      const operatorIndex = choiceOperator(segment);
      if (operatorIndex < 0) {
        fail(
          "malformed Java MessageFormat choice branch: expected limit#format, limit≤format, or limit<format",
        );
        continue;
      }

      const rawLimit = segment.slice(0, operatorIndex).trim();
      if (!CHOICE_LIMIT.test(rawLimit)) {
        fail(`invalid Java MessageFormat choice limit '${rawLimit}'`);
        continue;
      }

      const operator = segment[operatorIndex];
      const current = { value: choiceLimitValue(rawLimit), rank: operator === "<" ? 1 : 0 };
      if (
        previous &&
        (current.value < previous.value ||
          (current.value === previous.value && current.rank <= previous.rank))
      ) {
        fail("Java MessageFormat choice limits must be in ascending order");
      }
      previous = current;
      // ChoiceFormat removes its own apostrophe quoting before MessageFormat
      // reparses a selected branch containing an opening brace.
      const branch = decodeChoiceText(segment.slice(operatorIndex + 1));
      if (branch.includes("{")) scanMessage(branch);
    }
  }

  function parseArgument(text, open) {
    const close = matchingBrace(text, open + 1);
    if (close < 0) return text.length;

    const [rawIndex, rawType, style] = splitArgument(text.slice(open + 1, close));
    const indexText = rawIndex.trim();
    if (!/^\d+$/.test(indexText)) {
      fail("Java MessageFormat argument index must be a non-negative integer");
      return close + 1;
    }

    const index = Number(indexText);
    if (!Number.isSafeInteger(index) || index > 2_147_483_647) {
      fail("Java MessageFormat argument index is out of range");
      return close + 1;
    }

    if (rawType === undefined || rawType.trim() === "") {
      parts.push(String(index));
      return close + 1;
    }

    const type = rawType.trim();
    if (!MESSAGE_FORMAT_TYPES.has(type)) {
      fail(`unknown Java MessageFormat type '${type}'`);
      return close + 1;
    }
    parts.push(`${index}:${type}`);

    if (type === "choice") {
      if (style === undefined || style.trim() === "") {
        fail("Java MessageFormat choice argument requires a style");
      } else {
        parseChoice(style);
      }
    }
    return close + 1;
  }

  function scanMessageStep(text, index) {
    const ch = text[index];
    switch (ch) {
      case "'": {
        return skipQuote(text, index);
      }
      case "{": {
        return parseArgument(text, index);
      }
      case "}": {
        fail("unexpected '}' in Java MessageFormat pattern");
        return index + 1;
      }
      default: {
        return index + 1;
      }
    }
  }

  function scanMessage(text) {
    let index = 0;
    while (index < text.length) {
      index = scanMessageStep(text, index);
    }
  }

  scanMessage(s);
  return { parts: parts.sort(compareOrdinal), error };
}

// ── markup ─────────────────────────────────────────────────────────────────

const TAG = /<\/?\s*([A-Za-z][\w-]*)/g;

// Set of tag names (lowercased) present in the string. Compared by presence,
// not count, to avoid flagging a translator who legitimately merges two spans.
function markupSignature(s) {
  const tags = new Set();
  let m;
  while ((m = TAG.exec(s)) !== null) {
    tags.add(m[1].toLowerCase());
  }
  return [...tags].sort(compareOrdinal);
}

// ── unified ──────────────────────────────────────────────────────────────────

// Signature for one string under a given syntax:
//   { placeholders: string[], markup: string[], error: string|null }
export function signatureFor(syntax, s) {
  let ph;
  let markup;
  switch (syntax) {
    case "icu": {
      const scanned = scanIcu(s);
      ph = icuSignatureFromScan(scanned);
      markup = [...scanned.tags].sort(compareOrdinal);
      break;
    }
    case "messageformat": {
      ph = messageFormatSignature(s);
      break;
    }
    case "polyglot": {
      ph = polyglotSignature(s);
      break;
    }
    default: {
      ph = sprintfSignature(s);
    }
  }
  return {
    placeholders: [...new Set(ph.parts)],
    markup: markup ?? markupSignature(s),
    error: ph.error,
  };
}

// Set-difference of two signatures (which of `source` is missing from `locale`,
// and which `locale` parts are extra). ICU translations may add exact-number
// branches to an existing plural argument; every source selector stays required.
export function diffSignature(sourceParts, localeParts, { syntax } = {}) {
  const sourceSet = new Set(sourceParts);
  const localeSet = new Set(localeParts);
  return {
    missing: sourceParts.filter((p) => !localeSet.has(p)),
    extra: localeParts.filter((part) => {
      if (sourceSet.has(part)) return false;
      const exactPlural = syntax === "icu" && /^(.+:(?:plural|selectordinal)):=/.exec(part);
      return !exactPlural || !sourceSet.has(exactPlural[1]);
    }),
  };
}

export {
  icuSignature,
  markupSignature,
  messageFormatSignature,
  polyglotSignature,
  sprintfSignature,
};
