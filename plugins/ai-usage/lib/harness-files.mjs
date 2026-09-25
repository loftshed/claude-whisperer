import { copyFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";

/**
 * Config-file registration for OpenCode, which has no `mcp add` command. Every edit backs the file up once
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
 * Adds or removes `name` under the `section` object of a plain-JSON config such as OpenCode's.
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
