// Codec for Java .properties files, matching java.util.Properties
// logical-line semantics: # and ! comments, the first unescaped =/:/whitespace
// key separator, escaped separators, leading-whitespace stripping, odd-backslash
// line continuations with continuation-indentation stripping, \t \n \r \f and
// \uXXXX escapes, and last-wins duplicate keys.
//
// Byte decoding mirrors Java 9+ PropertyResourceBundle: fatal UTF-8 first,
// ISO-8859-1 when the bytes are not valid UTF-8. Never decodes with a lossy
// default.

const KEY_TERMINATORS = new Set(["=", ":", " ", "\t", "\f"]);

// Decode a properties Buffer. Returns { text, encoding: "utf8" | "latin1" }.
export function decodeProperties(buffer) {
  try {
    // PropertyResourceBundle retains U+FEFF in the first key; suppress the
    // decoder's automatic BOM removal to keep the same lookup identity.
    return {
      text: new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(buffer),
      encoding: "utf8",
    };
  } catch {
    return { text: buffer.toString("latin1"), encoding: "latin1" };
  }
}

function stripLeadingWhitespace(line) {
  let start = 0;
  while (start < line.length && [" ", "\t", "\f"].includes(line[start])) {
    start += 1;
  }
  return line.slice(start);
}

function endsWithOddBackslashes(line) {
  let count = 0;
  for (let index = line.length - 1; index >= 0 && line[index] === "\\"; index -= 1) count += 1;
  return count % 2 === 1;
}

// Resolve one \escaped char at `index` (just past the backslash+escaped-char
// pair) and return the decoded text plus the index to resume from. Extracted
// out of unescapeText's loop so its `break`s aren't nested inside that loop.
function resolveEscape(text, index, escaped, startLine) {
  switch (escaped) {
    case "u": {
      const hex = text.slice(index, index + 4);
      if (!/^[\dA-Fa-f]{4}$/.test(hex)) {
        throw new Error(String.raw`malformed \u escape at line ${startLine}`);
      }
      // Each \uXXXX is one UTF-16 code unit (max 0xFFFF), so fromCodePoint
      // emits it verbatim, including the halves of a surrogate pair.
      return { text: String.fromCodePoint(Number.parseInt(hex, 16)), nextIndex: index + 4 };
    }
    case "t": {
      return { text: "\t", nextIndex: index };
    }
    case "n": {
      return { text: "\n", nextIndex: index };
    }
    case "r": {
      return { text: "\r", nextIndex: index };
    }
    case "f": {
      return { text: "\f", nextIndex: index };
    }
    default: {
      return { text: escaped ?? "", nextIndex: index };
    }
  }
}

// Resolve \t \n \r \f \uXXXX and pass-through escapes. Surrogate pairs arrive
// as two \uXXXX escapes and combine into one code point in the JS string.
function unescapeText(text, startLine) {
  let out = "";
  let index = 0;
  while (index < text.length) {
    const ch = text[index];
    if (ch !== "\\") {
      out += ch;
      index += 1;
      continue;
    }
    const escaped = text[index + 1];
    const resolved = resolveEscape(text, index + 2, escaped, startLine);
    out += resolved.text;
    index = resolved.nextIndex;
  }
  return out;
}

// Split one logical line (leading whitespace already stripped, escapes intact)
// at the first unescaped =, :, or whitespace run; an unescaped =/: directly
// after that run still belongs to the separator.
function splitKeyValue(line, startLine) {
  let keyEnd = line.length;
  let isPrecedingBackslash = false;
  let scan = 0;
  while (scan < line.length) {
    const ch = line[scan];
    if (!isPrecedingBackslash && KEY_TERMINATORS.has(ch)) {
      keyEnd = scan;
      break;
    }
    isPrecedingBackslash = ch === "\\" ? !isPrecedingBackslash : false;
    scan += 1;
  }

  let valueStart = keyEnd;
  let isSeparatorSeen = false;
  while (valueStart < line.length) {
    const ch = line[valueStart];
    if (ch !== " " && ch !== "\t" && ch !== "\f") {
      if (isSeparatorSeen || (ch !== "=" && ch !== ":")) break;
      isSeparatorSeen = true;
    }
    valueStart += 1;
  }

  return {
    key: unescapeText(line.slice(0, keyEnd), startLine),
    value: unescapeText(line.slice(valueStart), startLine),
  };
}

// Parse a properties Buffer into:
//   values      Map<key, decoded value>; on duplicates the last value wins
//   locations   Map<key, { startLine, endLine }>, 1-based physical span of the
//               winning logical line
//   duplicates  [{ key, startLine, endLine }] shadowed earlier occurrences
//   encoding    "utf8" | "latin1"
//   newline     "\r\n" | "\r" | "\n"
export function parseProperties(buffer) {
  const { text, encoding } = decodeProperties(buffer);
  const newline = text.match(/\r\n|\r|\n/)?.[0] ?? "\n";
  const physicalLines = text.split(/\r\n|\r|\n/);

  const values = new Map();
  const locations = new Map();
  const duplicates = [];

  let lineNumber = 0;
  while (lineNumber < physicalLines.length) {
    let line = stripLeadingWhitespace(physicalLines[lineNumber]);
    const startLine = lineNumber + 1;
    let endLine = startLine;
    lineNumber += 1;

    if (line.length === 0 || line[0] === "#" || line[0] === "!") continue;

    // A continuation line is never a comment; its leading whitespace is
    // stripped before it is appended.
    while (endsWithOddBackslashes(line) && lineNumber < physicalLines.length) {
      line = line.slice(0, -1) + stripLeadingWhitespace(physicalLines[lineNumber]);
      endLine = lineNumber + 1;
      lineNumber += 1;
    }
    if (endsWithOddBackslashes(line)) line = line.slice(0, -1);

    const { key, value } = splitKeyValue(line, startLine);
    if (values.has(key)) duplicates.push({ key, ...locations.get(key) });
    values.set(key, value);
    locations.set(key, { startLine, endLine });
  }

  return { values, locations, duplicates, encoding, newline };
}

function unicodeEscape(codeUnit) {
  return String.raw`\u${codeUnit.toString(16).padStart(4, "0")}`;
}

function encodePropertiesText(text, { key }) {
  let output = "";
  let index = 0;
  while (index < text.length) {
    const ch = text[index];
    const codeUnit = ch.codePointAt(0);
    output += encodePropertiesChar(ch, index, codeUnit, key);
    index += 1;
  }
  return output;
}

// Encode one character. Extracted out of encodePropertiesText's loop so the
// switch's `break`s aren't nested inside that loop.
function encodePropertiesChar(ch, index, codeUnit, key) {
  switch (ch) {
    case "\\": {
      return "\\\\";
    }
    case "\t": {
      return String.raw`\t`;
    }
    case "\n": {
      return String.raw`\n`;
    }
    case "\r": {
      return String.raw`\r`;
    }
    case "\f": {
      return String.raw`\f`;
    }
    case "=":
    case ":": {
      return key ? `\\${ch}` : ch;
    }
    case "#":
    case "!": {
      return key && index === 0 ? `\\${ch}` : ch;
    }
    case " ": {
      return key || index === 0 ? String.raw`\ ` : " ";
    }
    default: {
      return codeUnit < 32 || codeUnit > 126 ? unicodeEscape(codeUnit) : ch;
    }
  }
}

export function encodePropertyKey(key) {
  return encodePropertiesText(key, { key: true });
}

export function encodePropertyValue(value) {
  return encodePropertiesText(value, { key: false });
}

export function encodePropertyLine(key, value) {
  return `${encodePropertyKey(key)}=${encodePropertyValue(value)}`;
}

function lineContentSpan(buffer, startLine, endLine) {
  let line = 1;
  let start = 0;
  let end = buffer.length;
  let index = 0;

  while (index < buffer.length) {
    if (line === startLine) {
      start = index;
      break;
    }
    if (buffer[index] === 10) {
      line += 1;
      index += 1;
    } else if (buffer[index] === 13) {
      line += 1;
      index += buffer[index + 1] === 10 ? 2 : 1;
    } else {
      index += 1;
    }
  }

  index = start;
  line = startLine;
  while (index < buffer.length) {
    if (buffer[index] === 10 || buffer[index] === 13) {
      if (line === endLine) {
        end = index;
        break;
      }
      line += 1;
      index += buffer[index] === 13 && buffer[index + 1] === 10 ? 2 : 1;
    } else {
      index += 1;
    }
  }
  return { start, end };
}

export function appendProperty(buffer, parsed, key, value) {
  const newline = Buffer.from(parsed.newline, "ascii");
  const line = Buffer.from(`${encodePropertyLine(key, value)}${parsed.newline}`, "ascii");
  const isEndsWithNewline = buffer.length === 0 || buffer.at(-1) === 10 || buffer.at(-1) === 13;
  // A final continuation consumes the next physical line. Terminate it with
  // an empty line before adding a key, preserving the existing bytes and value.
  const lastLine = buffer
    .toString("latin1")
    .replace(/\r\n$|[\r\n]$/, "")
    .split(/\r\n|[\r\n]/)
    .at(-1);
  const separator = isEndsWithNewline ? [] : [newline];
  if (endsWithOddBackslashes(lastLine)) separator.push(newline);
  return Buffer.concat([buffer, ...separator, line]);
}

export function replaceProperty(buffer, parsed, key, value) {
  const location = parsed.locations.get(key);
  if (!location) throw new Error(`property ${key} not found`);
  const { start, end } = lineContentSpan(buffer, location.startLine, location.endLine);
  const replacement = Buffer.from(encodePropertyLine(key, value), "ascii");
  return Buffer.concat([buffer.subarray(0, start), replacement, buffer.subarray(end)]);
}
