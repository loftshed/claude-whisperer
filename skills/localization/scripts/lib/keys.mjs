// The common dotted namespace shared by every key, excluding each key's final
// segment so a single key still displays in full.
export function sharedNamespace(keys) {
  if (keys.length === 0) return "";
  const parts = keys.map((key) => key.split("."));
  let length = 0;
  while (
    length < parts[0].length - 1 &&
    parts.every((segments) => segments[length] === parts[0][length])
  ) {
    length += 1;
  }
  return parts[0].slice(0, length).join(".");
}
