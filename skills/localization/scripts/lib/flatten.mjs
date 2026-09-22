// Flatten nested translation JSON to a Map of dotted-key -> string leaf.
// Nested objects and flat dotted keys collapse to the same dotted-key space,
// including comparisons with catalogs from before a format migration.
export function flatten(object, prefix, out) {
  if (typeof object === "string") {
    out.set(prefix, object);
    return out;
  }
  if (object && typeof object === "object" && !Array.isArray(object)) {
    for (const [k, v] of Object.entries(object)) {
      flatten(v, prefix ? `${prefix}.${k}` : k, out);
    }
    return out;
  }
  if (Array.isArray(object)) {
    for (const [index, v] of object.entries()) flatten(v, `${prefix}[${index}]`, out);
  }
  return out;
}
