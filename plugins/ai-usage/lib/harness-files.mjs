import { copyFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";

/**
 * Config-file registrations for harnesses without an `mcp add` command. Every edit backs the file up once
 * (`<file>.bak-ai-usage`), re-parses the result before writing, and never prints file contents: these
 * files can hold API keys.
 */

export const stripJsonc = (text) =>
  text.replace(/("(?:\\.|[^"\\])*")|\/\/[^\n]*|\/\*[\s\S]*?\*\//g, (m, str) => str ?? "").replace(/,(\s*[}\]])/g, "$1");

const indentUnit = (text) => (text.match(/\n([ \t]+)"/) || [, "  "])[1];
const hasComments = (text) => stripJsonc(text).length !== text.replace(/,(\s*[}\]])/g, "$1").length;

function backup(file) {
  const copy = `${file}.bak-ai-usage`;
  if (!existsSync(copy)) copyFileSync(file, copy);
}

/**
 * Adds or removes `name` under the `section` object of a plain-JSON config (OpenCode, Claude Desktop).
 * Refuses files with comments, since re-serialising would drop them.
 */
export function editJsonSection(file, section, name, entry, { remove = false } = {}) {
  const text = existsSync(file) ? readFileSync(file, "utf8") : "{}\n";
  if (hasComments(text)) return { changed: false, manual: `${file} has comments; add "${name}" under "${section}" by hand` };
  const data = JSON.parse(stripJsonc(text));
  const present = Boolean(data[section]?.[name]);
  if (remove ? !present : present) return { changed: false };
  if (remove) delete data[section][name];
  else (data[section] ??= {})[name] = entry;
  const next = `${JSON.stringify(data, null, indentUnit(text))}\n`;
  JSON.parse(next);
  if (existsSync(file)) backup(file);
  writeFileSync(file, next);
  return { changed: true };
}

/**
 * Zed keeps settings.json as JSONC with comments, so insert the entry as text right after the single
 * `"context_servers": {` line, matching the file's indentation, and verify the result still parses.
 */
export function editZedContextServer(file, name, entry, { remove = false } = {}) {
  if (!existsSync(file)) return { changed: false, manual: `${file} not found` };
  const text = readFileSync(file, "utf8");
  const present = Boolean(JSON.parse(stripJsonc(text)).context_servers?.[name]);
  if (remove) {
    if (!present) return { changed: false };
    const block = new RegExp(`^[ \\t]*"${name}": \\{\\n(?:[^\\n]*\\n)*?[ \\t]*\\},?\\n`, "m");
    const next = text.replace(block, "");
    if (next === text || JSON.parse(stripJsonc(next)).context_servers?.[name]) return { changed: false, manual: `remove "${name}" from context_servers in ${file} by hand` };
    backup(file);
    writeFileSync(file, next);
    return { changed: true };
  }
  if (present) return { changed: false };
  const anchors = text.match(/^[ \t]*"context_servers": \{\n/gm) ?? [];
  if (anchors.length !== 1) return { changed: false, manual: `add "${name}" under "context_servers" in ${file} by hand` };
  const unit = indentUnit(text);
  const next = text.replace(/^([ \t]*)"context_servers": \{\n/m, (line, base) => {
    const i1 = base + unit;
    const i2 = i1 + unit;
    const fields = Object.entries(entry).map(([k, v]) => `${i2}${JSON.stringify(k)}: ${JSON.stringify(v)}`);
    return `${line}${i1}${JSON.stringify(name)}: {\n${fields.join(",\n")}\n${i1}},\n`;
  });
  if (!JSON.parse(stripJsonc(next)).context_servers?.[name]) return { changed: false, manual: `add "${name}" under "context_servers" in ${file} by hand` };
  backup(file);
  writeFileSync(file, next);
  return { changed: true };
}
