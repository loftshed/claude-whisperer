// Locate where each translation key sits in the *raw* JSON text. JSON.parse
// discards line numbers, so this re-scans the source: a tiny tokenizer plus a
// structural walk that records the (path, line, column) of every key. It is the
// position-aware sibling of flatten.mjs and collapses to the same dotted-key
// space. A nested object path and a literal dotted key resolve identically,
// including in catalogs from before a format migration.

// Scan a JSON string literal's body starting right after the opening quote,
// keeping escape pairs intact so `\"` doesn't terminate the string early.
// Returns the raw (still-escaped) body text plus the index/line/col right
// after the closing quote (or wherever scanning stopped, if unterminated).
function scanJsonStringBody(text, n, index, line, col) {
  let raw = "";
  while (index < n) {
    const c = text[index];
    if (c === "\\") {
      raw += c + (text[index + 1] ?? "");
      index += 2;
      col += 2;
      continue;
    }
    if (c === '"') {
      index += 1;
      col += 1;
      break;
    }
    if (c === "\n") {
      line += 1;
      col = 1;
    } else {
      col += 1;
    }
    raw += c;
    index += 1;
  }
  return { raw, index, line, col };
}

// Tokenize JSON into structural punctuation and strings, each carrying the
// 1-based line/column where it starts. Scalars (number/bool/null) collapse to a
// single "literal" token since only their position matters here, never content.
function tokenize(text) {
  const tokens = [];
  let index = 0;
  let line = 1;
  let col = 1;
  const n = text.length;

  while (index < n) {
    const ch = text[index];
    if (ch === "\n") {
      line += 1;
      col = 1;
      index += 1;
      continue;
    }
    if ([" ", "\t", "\r"].includes(ch)) {
      index += 1;
      col += 1;
      continue;
    }
    if (["{", "}", "[", "]", ":", ","].includes(ch)) {
      tokens.push({ type: ch, line, col });
      index += 1;
      col += 1;
      continue;
    }
    if (ch === '"') {
      const startLine = line;
      const startCol = col;
      index += 1;
      col += 1;
      const scanned = scanJsonStringBody(text, n, index, line, col);
      const { raw } = scanned;
      ({ index, line, col } = scanned);
      let value;
      try {
        value = JSON.parse(`"${raw}"`);
      } catch {
        value = raw;
      }
      tokens.push({ type: "string", value, line: startLine, col: startCol });
      continue;
    }
    // Number, true, false, null — consume up to the next delimiter/space.
    const startLine = line;
    const startCol = col;
    let lit = "";
    while (index < n && !"{}[]:, \t\r\n".includes(text[index])) {
      lit += text[index];
      index += 1;
      col += 1;
    }
    tokens.push({ type: "literal", value: lit, line: startLine, col: startCol });
  }
  return tokens;
}

// Walk the token stream and emit one entry per key position:
//   { path, line, column, kind, value? }
// kind is "string" | "literal" | "object" | "array". Container keys are emitted
// too (their opening line), so an intermediate path like aria_label resolves to
// the line its subtree opens on. Array elements use flatten's `prefix[idx]` form.
export function locate(text) {
  // Validate before the positional walk: incomplete containers have no safe
  // token boundary to advance through and must never yield partial evidence.
  JSON.parse(text);
  const tokens = tokenize(text);
  const out = [];
  let pos = 0;

  const peek = () => tokens[pos];

  function recordValue(entry, prefix) {
    const tok = peek();
    if (!tok) return;
    if (tok.type === "string" || tok.type === "literal") {
      entry.kind = tok.type;
      entry.value = tok.value;
      out.push(entry);
      pos += 1;
      return;
    }
    if (tok.type === "{" || tok.type === "[") {
      entry.kind = tok.type === "{" ? "object" : "array";
      out.push(entry);
      parseContainer(prefix);
      return;
    }
    out.push(entry);
  }

  function parseContainer(prefix) {
    const open = peek();
    if (!open || (open.type !== "{" && open.type !== "[")) return;
    pos += 1;
    const close = open.type === "{" ? "}" : "]";

    if (open.type === "{") {
      while (peek() && peek().type !== close) {
        const keyTok = peek();
        if (keyTok.type !== "string") break; // malformed; stop this container
        pos += 1;
        if (peek() && peek().type === ":") pos += 1;
        const childPath = prefix ? `${prefix}.${keyTok.value}` : keyTok.value;
        recordValue({ path: childPath, line: keyTok.line, column: keyTok.col }, childPath);
        if (peek() && peek().type === ",") pos += 1;
      }
    } else {
      let index = 0;
      while (peek() && peek().type !== close) {
        const valueTok = peek();
        const childPath = `${prefix}[${index}]`;
        recordValue({ path: childPath, line: valueTok.line, column: valueTok.col }, childPath);
        if (peek() && peek().type === ",") pos += 1;
        index += 1;
      }
    }
    if (peek() && peek().type === close) pos += 1;
  }

  if (peek() && (peek().type === "{" || peek().type === "[")) {
    parseContainer("");
  }
  return out;
}

// Exact-path matches for a requested dotted path, in document order.
export function findPath(text, path) {
  return locate(text).filter((e) => e.path === path);
}

// Keys whose path starts with `${prefix}.` — used to suggest drill-down targets
// when an exact path misses (e.g. the caller named an intermediate or mistyped).
export function childrenOf(text, prefix) {
  const needle = `${prefix}.`;
  return locate(text).filter((e) => e.path.startsWith(needle));
}
