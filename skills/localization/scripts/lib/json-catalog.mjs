import { flatten } from "./flatten.mjs";
import { locate } from "./locate.mjs";

// Preserve duplicate occurrences before JSON.parse's last-value-wins result
// reaches a writer or a structural check. Container duplicates matter too:
// replacing an object can discard an entire subtree of messages.
export function parseJsonCatalog(buffer) {
  const raw = buffer.toString("utf8");
  const object = JSON.parse(raw);
  if (!object || typeof object !== "object" || Array.isArray(object)) {
    throw new Error("JSON catalog must contain an object");
  }
  const locations = new Map();
  const duplicates = [];
  for (const entry of locate(raw)) {
    const location = { startLine: entry.line, endLine: entry.line };
    if (locations.has(entry.path)) duplicates.push({ key: entry.path, ...location });
    locations.set(entry.path, location);
  }
  return { object, values: flatten(object, "", new Map()), locations, duplicates };
}
