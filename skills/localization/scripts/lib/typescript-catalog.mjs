const IDENTIFIER = /^[A-Za-z_$][\w$]*$/;

function isToken(token, value) {
  return token?.type !== "string" && token?.value === value;
}

function isExpressionEnd(tokens, index, previous) {
  while (isToken(tokens[index], "as") && isToken(tokens[index + 1], "const")) {
    previous = tokens[index + 1];
    index += 2;
  }
  const token = tokens[index];
  if (!token || isToken(token, ";") || isToken(token, ",")) return true;
  return (
    token.line > previous.endLine &&
    token.type === "identifier" &&
    ["const", "let", "var", "function", "class", "export", "import", "interface", "type"].includes(
      token.value,
    )
  );
}

export class TypeScriptCatalogError extends Error {
  constructor(message, line = null) {
    super(line === null ? message : `${message} at line ${line}`);
    this.name = "TypeScriptCatalogError";
  }
}

function lineStarts(text) {
  const starts = [0];
  let index = text.indexOf("\n");
  while (index !== -1) {
    starts.push(index + 1);
    index = text.indexOf("\n", index + 1);
  }
  return starts;
}

function positionAt(starts, index) {
  let low = 0;
  let high = starts.length;
  while (low + 1 < high) {
    const middle = Math.floor((low + high) / 2);
    if (starts[middle] <= index) low = middle;
    else high = middle;
  }
  return { line: low + 1, column: index - starts[low] + 1 };
}

function decodeEscape(raw, index, line) {
  const ch = raw[index];
  if (/[1-9]/.test(ch) || (ch === "0" && /\d/.test(raw[index + 1] ?? ""))) {
    throw new TypeScriptCatalogError(
      "legacy numeric string escapes are not valid in a module",
      line,
    );
  }
  const simple = {
    b: "\b",
    f: "\f",
    n: "\n",
    r: "\r",
    t: "\t",
    v: "\v",
    0: "\0",
  };
  if (Object.hasOwn(simple, ch)) return { value: simple[ch], consumed: 1 };
  if (ch === "\n") return { value: "", consumed: 1 };
  if (ch === "\r") {
    return { value: "", consumed: raw[index + 1] === "\n" ? 2 : 1 };
  }
  if (ch === "x") {
    const digits = raw.slice(index + 1, index + 3);
    if (!/^[\dA-Fa-f]{2}$/.test(digits)) {
      throw new TypeScriptCatalogError("malformed hexadecimal string escape", line);
    }
    return { value: String.fromCodePoint(Number.parseInt(digits, 16)), consumed: 3 };
  }
  if (ch === "u") {
    if (raw[index + 1] === "{") {
      const close = raw.indexOf("}", index + 2);
      const digits = close === -1 ? "" : raw.slice(index + 2, close);
      if (!/^[\dA-Fa-f]{1,6}$/.test(digits)) {
        throw new TypeScriptCatalogError("malformed Unicode string escape", line);
      }
      const codePoint = Number.parseInt(digits, 16);
      if (codePoint > 1_114_111) {
        throw new TypeScriptCatalogError("Unicode string escape is out of range", line);
      }
      return { value: String.fromCodePoint(codePoint), consumed: close - index + 1 };
    }
    const digits = raw.slice(index + 1, index + 5);
    if (!/^[\dA-Fa-f]{4}$/.test(digits)) {
      throw new TypeScriptCatalogError("malformed Unicode string escape", line);
    }
    return { value: String.fromCodePoint(Number.parseInt(digits, 16)), consumed: 5 };
  }
  return { value: ch, consumed: 1 };
}

function decodeString(raw, line) {
  let value = "";
  for (let index = 0; index < raw.length; index += 1) {
    if (raw[index] !== "\\") {
      value += raw[index];
      continue;
    }
    if (index + 1 >= raw.length) {
      throw new TypeScriptCatalogError("unterminated string escape", line);
    }
    const decoded = decodeEscape(raw, index + 1, line);
    value += decoded.value;
    index += decoded.consumed;
  }
  return value;
}

// Scan a quoted string literal's body starting right after the opening quote,
// consuming backslash escapes and (for template literals) noting `${`
// interpolation. Returns the raw (still-escaped) body text, whether it was
// closed, and the index right after the closing quote (or `text.length` if
// unterminated).
function scanQuotedStringBody(text, quote, start, starts) {
  let index = start;
  let raw = "";
  let isInterpolated = false;
  let isClosed = false;
  while (index < text.length) {
    const current = text[index];
    if (current === "\\") {
      raw += current;
      if (index + 1 < text.length) {
        raw += text[index + 1];
        if (text[index + 1] === "\r" && text[index + 2] === "\n") {
          raw += "\n";
          index += 3;
        } else {
          index += 2;
        }
      } else {
        index += 1;
      }
      continue;
    }
    if (quote !== "`" && (current === "\n" || current === "\r")) {
      throw new TypeScriptCatalogError(
        "raw newline in quoted string literal",
        positionAt(starts, index).line,
      );
    }
    if (quote === "`" && current === "$" && text[index + 1] === "{") {
      isInterpolated = true;
    }
    if (quote === "`" && current === "\r") {
      raw += "\n";
      index += text[index + 1] === "\n" ? 2 : 1;
      continue;
    }
    if (current === quote) {
      index += 1;
      isClosed = true;
      break;
    }
    raw += current;
    index += 1;
  }
  return { raw, isInterpolated, isClosed, end: index };
}

function tokenize(text) {
  const tokens = [];
  const starts = lineStarts(text);
  let index = 0;

  const push = (type, value, start, end, extra = {}) => {
    const position = positionAt(starts, start);
    const endPosition = positionAt(starts, Math.max(start, end - 1));
    tokens.push({ type, value, start, end, endLine: endPosition.line, ...position, ...extra });
  };

  while (index < text.length) {
    const ch = text[index];
    if (/\s/.test(ch)) {
      index += 1;
      continue;
    }
    if (ch === "/" && text[index + 1] === "/") {
      const newline = text.indexOf("\n", index + 2);
      index = newline === -1 ? text.length : newline + 1;
      continue;
    }
    if (ch === "/" && text[index + 1] === "*") {
      const close = text.indexOf("*/", index + 2);
      if (close === -1) {
        throw new TypeScriptCatalogError(
          "unterminated block comment",
          positionAt(starts, index).line,
        );
      }
      index = close + 2;
      continue;
    }
    if (text.startsWith("...", index)) {
      push("punctuation", "...", index, index + 3);
      index += 3;
      continue;
    }
    if (/[A-Za-z_$]/.test(ch)) {
      const start = index;
      index += 1;
      while (index < text.length && /[\w$]/.test(text[index])) index += 1;
      push("identifier", text.slice(start, index), start, index);
      continue;
    }
    if (["'", '"', "`"].includes(ch)) {
      const quote = ch;
      const start = index;
      const scanned = scanQuotedStringBody(text, quote, start + 1, starts);
      index = scanned.end;
      const line = positionAt(starts, start).line;
      if (!scanned.isClosed) throw new TypeScriptCatalogError("unterminated string literal", line);
      push("string", decodeString(scanned.raw, line), start, index, {
        quote,
        interpolated: scanned.isInterpolated,
      });
      continue;
    }
    if (/\d/.test(ch)) {
      const start = index;
      index += 1;
      while (index < text.length && /[\d.eE_+-]/.test(text[index])) index += 1;
      push("number", text.slice(start, index), start, index);
      continue;
    }
    push("punctuation", ch, index, index + 1);
    index += 1;
  }

  return tokens;
}

function pathKey(segments) {
  return JSON.stringify(segments);
}

function flatKey(segments) {
  return segments.join(".");
}

function isIgnored(catalog, key) {
  return (catalog.ignoreKeyPrefixes ?? []).some(
    (prefix) => key === prefix || key.startsWith(`${prefix}.`),
  );
}

function findMatching(tokens, start, open, close) {
  let depth = 0;
  for (let index = start; index < tokens.length; index += 1) {
    if (isToken(tokens[index], open)) depth += 1;
    else if (isToken(tokens[index], close)) {
      depth -= 1;
      if (depth === 0) return index;
    }
  }
  return -1;
}

function findInitializerIndex(tokens, index) {
  while (index < tokens.length && !isToken(tokens[index], "=") && !isToken(tokens[index], ";")) {
    index += 1;
  }
  return isToken(tokens[index], "=") ? index + 1 : -1;
}

function collectConstants(tokens) {
  const constants = new Map();
  let depth = 0;
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (token.type === "punctuation") {
      if (["{", "[", "("].includes(token.value)) depth += 1;
      else if (["}", "]", ")"].includes(token.value)) depth -= 1;
    }
    if (depth !== 0 || !isToken(token, "const") || tokens[index + 1]?.type !== "identifier") {
      continue;
    }
    const name = tokens[index + 1].value;
    const initializer = findInitializerIndex(tokens, index + 2);
    if (initializer < 0) continue;
    const valueToken = tokens[initializer];
    if (!valueToken || !isExpressionEnd(tokens, initializer + 1, valueToken)) continue;
    if (valueToken.type === "string" && !valueToken.interpolated) {
      constants.set(name, { value: valueToken.value, token: valueToken });
    } else if (valueToken.type === "identifier" && constants.has(valueToken.value)) {
      constants.set(name, constants.get(valueToken.value));
    }
  }
  return constants;
}

function exportedMessagesObject(tokens) {
  for (let index = 0; index < tokens.length - 3; index += 1) {
    if (
      !isToken(tokens[index], "export") ||
      !isToken(tokens[index + 1], "const") ||
      !isToken(tokens[index + 2], "messages")
    ) {
      continue;
    }
    const initializer = findInitializerIndex(tokens, index + 3);
    if (isToken(tokens[initializer], "{")) return initializer;
  }
  return -1;
}

export function parseTypeScriptCatalog(buffer, catalog = {}) {
  const text = Buffer.isBuffer(buffer) ? buffer.toString("utf8") : String(buffer);
  const tokens = tokenize(text);
  const constants = collectConstants(tokens);
  const values = new Map();
  const locations = new Map();
  const duplicates = [];
  const objects = new Map();
  const mergeObjects = new Map();
  const definedProperties = new Set();
  const rootIndex = exportedMessagesObject(tokens);
  if (rootIndex < 0) {
    throw new TypeScriptCatalogError("expected `export const messages = { ... }`");
  }

  const addValue = (segments, value, location, container) => {
    const key = flatKey(segments);
    if (isIgnored(catalog, key)) return;
    definedProperties.add(pathKey(segments));
    if (values.has(key)) {
      duplicates.push({
        key,
        startLine: location.line,
        endLine: location.endLine,
      });
    }
    values.set(key, value);
    locations.set(key, { ...location, key, segments, container });
  };

  const resolveTokenValue = (token, key) => {
    if (token?.type === "string") {
      if (token.interpolated) {
        throw new TypeScriptCatalogError(
          `interpolated template literals are not supported for ${key}`,
          token.line,
        );
      }
      return { value: token.value, token, writable: true };
    }
    if (token?.type === "identifier" && constants.has(token.value)) {
      const constant = constants.get(token.value);
      return { value: constant.value, token: constant.token, writable: false };
    }
    throw new TypeScriptCatalogError(`expected a static string value for ${key}`, token?.line);
  };

  const parseGeneratedDictionary = (index, segments, container) => {
    const callee = tokens[index]?.value;
    const suffixes = catalog.generatedDictionaries?.[callee];
    if (tokens[index]?.type !== "identifier" || !suffixes || !isToken(tokens[index + 1], "(")) {
      return null;
    }
    const close = findMatching(tokens, index + 1, "(", ")");
    if (close < 0) {
      throw new TypeScriptCatalogError(`unterminated ${callee} call`, tokens[index].line);
    }
    const first = resolveTokenValue(tokens[index + 2], `${callee} key`);
    if (!isToken(tokens[index + 3], ",")) {
      throw new TypeScriptCatalogError(
        `${callee} requires key and value arguments`,
        tokens[index].line,
      );
    }
    const second = resolveTokenValue(tokens[index + 4], `${callee} value`);
    if (index + 5 !== close) {
      throw new TypeScriptCatalogError(
        `${callee} accepts two static string arguments`,
        tokens[index].line,
      );
    }
    for (const suffix of suffixes) {
      addValue(
        [...segments, `${first.value}.${suffix}`],
        second.value,
        {
          line: tokens[index].line,
          endLine: tokens[close].endLine,
          kind: "string",
          valueStart: second.token.start,
          valueEnd: second.token.end,
          quote: second.token.quote,
          writable: false,
          generated: true,
        },
        container,
      );
    }
    return close + 1;
  };

  const skipExpression = (index, stopValue) => {
    const pairs = new Map([
      ["(", ")"],
      ["[", "]"],
      ["{", "}"],
    ]);
    const stack = [];
    while (index < tokens.length) {
      const token = tokens[index];
      if (isToken(token, stopValue) && stack.length === 0) return index;
      if (token.type === "punctuation") {
        if (pairs.has(token.value)) stack.push(pairs.get(token.value));
        else if (stack.at(-1) === token.value) stack.pop();
      }
      index += 1;
    }
    return -1;
  };

  const replaceSubtree = (segments) => {
    const key = pathKey(segments);
    if (definedProperties.has(key)) {
      // Object properties replace whole subtrees, including objects produced
      // by a preceding spread. Keep only the value that the module exposes.
      const isWithinProperty = (candidate) =>
        segments.every((segment, position) => candidate[position] === segment);
      for (const [previousKey, location] of locations) {
        if (!isWithinProperty(location.segments)) {
          continue;
        }

        values.delete(previousKey);
        locations.delete(previousKey);
      }
      for (const previousObjects of [objects, mergeObjects]) {
        for (const [previousKey, previousNode] of previousObjects) {
          if (isWithinProperty(previousNode.segments)) previousObjects.delete(previousKey);
        }
      }
    }
    definedProperties.add(key);
  };

  const parseObject = (openIndex, segments, container = "messages") => {
    const open = tokens[openIndex];
    const node = { segments, open, close: null, properties: [] };
    const objectMap = container === "merge" ? mergeObjects : objects;
    objectMap.set(pathKey(segments), node);
    const propertyNames = new Set();
    let index = openIndex + 1;

    const consumeSeparator = (nextIndex, label) => {
      const next = tokens[nextIndex];
      if (isToken(next, ",")) return nextIndex + 1;
      if (isToken(next, "}")) return nextIndex;
      throw new TypeScriptCatalogError(
        `expected ',' or '}' after ${label}`,
        next?.line ?? open.line,
      );
    };

    while (index < tokens.length && !isToken(tokens[index], "}")) {
      const propertyStart = tokens[index];
      node.properties.push(propertyStart);

      if (isToken(propertyStart, "...")) {
        const generated = parseGeneratedDictionary(index + 1, segments, container);
        if (generated !== null) {
          index = consumeSeparator(generated, "generated dictionary spread");
          continue;
        }
        if (
          isToken(tokens[index + 1], "_") &&
          isToken(tokens[index + 2], ".") &&
          isToken(tokens[index + 3], "merge") &&
          isToken(tokens[index + 4], "(")
        ) {
          const comma = skipExpression(index + 5, ",");
          if (comma < 0 || !isToken(tokens[comma + 1], "{")) {
            throw new TypeScriptCatalogError(
              "merge spread requires a static local override object",
              propertyStart.line,
            );
          }
          const afterObject = parseObject(comma + 1, segments, "merge");
          if (!isToken(tokens[afterObject], ")")) {
            throw new TypeScriptCatalogError(
              "merge spread accepts one external dictionary and one local override object",
              propertyStart.line,
            );
          }
          index = consumeSeparator(afterObject + 1, "merge spread");
          continue;
        }
        throw new TypeScriptCatalogError(
          "unsupported spread in messages object",
          propertyStart.line,
        );
      }

      if (!["identifier", "string"].includes(propertyStart.type) || propertyStart.quote === "`") {
        throw new TypeScriptCatalogError("expected a static message key", propertyStart.line);
      }
      const propertyName = propertyStart.value;
      const propertySegments = [...segments, propertyName];
      const key = flatKey(propertySegments);
      if (propertyNames.has(propertyName) && !isIgnored(catalog, key)) {
        duplicates.push({ key, startLine: propertyStart.line, endLine: propertyStart.endLine });
      }
      propertyNames.add(propertyName);
      replaceSubtree(propertySegments);
      index += 1;

      const isShorthand = isToken(tokens[index], ",") || isToken(tokens[index], "}");
      if (isShorthand) {
        if (propertyStart.type !== "identifier") {
          throw new TypeScriptCatalogError(`expected ':' after ${key}`, propertyStart.line);
        }
      } else {
        if (!isToken(tokens[index], ":")) {
          throw new TypeScriptCatalogError(`expected ':' after ${key}`, tokens[index]?.line);
        }
        index += 1;
        if (isToken(tokens[index], "{")) {
          index = consumeSeparator(parseObject(index, propertySegments, container), key);
          continue;
        }
      }

      const resolved = resolveTokenValue(isShorthand ? propertyStart : tokens[index], key);
      addValue(
        propertySegments,
        resolved.value,
        {
          line: propertyStart.line,
          endLine: isShorthand ? propertyStart.endLine : resolved.token.endLine,
          kind: "string",
          valueStart: resolved.token.start,
          valueEnd: resolved.token.end,
          quote: resolved.token.quote,
          writable: !isShorthand && resolved.writable,
          generated: false,
        },
        container,
      );
      index = consumeSeparator(isShorthand ? index : index + 1, key);
    }

    if (!isToken(tokens[index], "}")) {
      throw new TypeScriptCatalogError("unterminated messages object", open.line);
    }
    node.close = tokens[index];
    return index + 1;
  };

  const afterObject = parseObject(rootIndex, []);
  if (!isExpressionEnd(tokens, afterObject, tokens[afterObject - 1])) {
    throw new TypeScriptCatalogError(
      "unsupported expression after messages object",
      tokens[afterObject]?.line,
    );
  }
  return {
    values,
    locations,
    duplicates,
    objects,
    mergeObjects,
    tokens,
    text,
    newline: text.includes("\r\n") ? "\r\n" : "\n",
  };
}

export function typeScriptEntries(parsed) {
  return parsed.values
    .entries()
    .map(([key, value]) => {
      const location = parsed.locations.get(key);
      return {
        path: key,
        line: location.line,
        endLine: location.endLine,
        kind: "string",
        value,
        generated: location.generated,
      };
    })
    .toArray();
}

function quoteString(value) {
  return `'${value
    .replaceAll("\\", "\\\\")
    .replaceAll("'", String.raw`\'`)
    .replaceAll("\b", String.raw`\b`)
    .replaceAll("\f", String.raw`\f`)
    .replaceAll("\n", String.raw`\n`)
    .replaceAll("\r", String.raw`\r`)
    .replaceAll("\t", String.raw`\t`)
    .replaceAll("\v", String.raw`\v`)
    .replaceAll("\u{2028}", String.raw`\u2028`)
    .replaceAll("\u{2029}", String.raw`\u2029`)}'`;
}

function quoteKey(key) {
  return IDENTIFIER.test(key) ? key : quoteString(key);
}

function lineStart(text, index) {
  const newline = text.lastIndexOf("\n", index - 1);
  return newline === -1 ? 0 : newline + 1;
}

function indentationAt(text, index) {
  const start = lineStart(text, index);
  const prefix = text.slice(start, index);
  return /^[\t ]*$/.test(prefix) ? prefix : null;
}

function renderProperty(segments, value, indent, unit, newline) {
  const [head, ...tail] = segments;
  if (tail.length === 0) return `${indent}${quoteKey(head)}: ${quoteString(value)},${newline}`;
  return `${indent}${quoteKey(head)}: {${newline}${renderProperty(
    tail,
    value,
    indent + unit,
    unit,
    newline,
  )}${indent}},${newline}`;
}

function insertIntoObject(parsed, node, remainingSegments, value, indentUnit) {
  const { text, newline, tokens } = parsed;
  const closeIndent = indentationAt(text, node.close.start);
  const previousToken = tokens.findLast(
    (token) => token.start > node.open.end && token.end <= node.close.start,
  );

  let childIndent = closeIndent === null ? indentUnit : closeIndent + indentUnit;
  const directProperty = node.properties.find((token) => indentationAt(text, token.start) !== null);
  if (directProperty) childIndent = indentationAt(text, directProperty.start);

  const rendered = renderProperty(remainingSegments, value, childIndent, indentUnit, newline);
  const closeLineStart = lineStart(text, node.close.start);
  const isMultilineClose = closeIndent !== null && closeLineStart > node.open.end;
  const insertionPoint = isMultilineClose ? closeLineStart : node.close.start;

  let before = text.slice(0, insertionPoint);
  const body = text.slice(node.open.end, node.close.start).trim();
  if (body !== "" && previousToken && !isToken(previousToken, ",")) {
    before = `${before.slice(0, previousToken.end)},${before.slice(previousToken.end)}`;
  }

  if (isMultilineClose) {
    return `${before}${rendered}${text.slice(insertionPoint)}`;
  }
  const separator = body === "" ? newline : " ";
  const closingIndent = closeIndent ?? "";
  return `${before}${separator}${rendered}${closingIndent}${text.slice(insertionPoint)}`;
}

export function setTypeScriptCatalogValue(
  buffer,
  parsed,
  key,
  value,
  { force = false, sourceLocation = null, indent = "  " } = {},
) {
  const existing = parsed.locations.get(key);
  if (existing) {
    if (!force && parsed.values.get(key) !== "") {
      throw new TypeScriptCatalogError(`refusing to overwrite ${key}`);
    }
    if (!existing.writable) {
      throw new TypeScriptCatalogError(`cannot edit generated or shared value ${key}`);
    }
    const output = `${parsed.text.slice(0, existing.valueStart)}${quoteString(value)}${parsed.text.slice(
      existing.valueEnd,
    )}`;
    return Buffer.from(output, "utf8");
  }

  const segments = sourceLocation?.segments ?? key.split(".");
  const container = sourceLocation?.container ?? "messages";
  const objectMap = container === "merge" ? parsed.mergeObjects : parsed.objects;
  const parent = segments.slice(0, -1);
  let ancestorLength = parent.length;
  let node = null;
  while (ancestorLength >= 0) {
    node = objectMap.get(pathKey(parent.slice(0, ancestorLength)));
    if (node) break;
    const blocked = parsed.locations.get(flatKey(parent.slice(0, ancestorLength)));
    if (blocked?.container === container) {
      throw new TypeScriptCatalogError(
        `cannot create ${key}: ${flatKey(parent.slice(0, ancestorLength))} is not an object`,
      );
    }
    ancestorLength -= 1;
  }
  if (!node) {
    const label = container === "merge" ? "merge override object" : "messages object";
    throw new TypeScriptCatalogError(`cannot find ${label} for ${key}`);
  }

  const output = insertIntoObject(parsed, node, segments.slice(ancestorLength), value, indent);
  return Buffer.from(output, "utf8");
}
