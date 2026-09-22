// Pure key-set helpers for scoping a run to the current branch's changes, shared by
// untranslated.mjs (discovery) and validate-structure.mjs (the gate). No git or I/O
// lives here — callers supply the parsed source Map and the diff text, so the git
// plumbing and each script's own error model stay in the script.

// Source keys added, changed, or removed vs the base map. Removed keys matter
// to structural validation because their surviving locale entries become
// branch-introduced stale keys.
export function changedSourceKeys(source, base) {
  const changed = new Set();
  for (const [key, value] of source.entries()) {
    if (!base.has(key) || base.get(key) !== value) changed.add(key);
  }
  for (const key of base.keys()) {
    if (!source.has(key)) changed.add(key);
  }
  return changed;
}

// Source keys whose literal dotted key appears in added diff text. The boundary guards
// keep a key from matching inside a longer key (e.g. `a.b` in `a.b.c`), so it only
// matches a real reference to that exact key.
export function newlyUsedKeys(source, addedText) {
  const used = new Set();
  if (!addedText) return used;
  for (const key of source.keys()) {
    const escaped = key.replaceAll(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`);
    if (new RegExp(String.raw`(?<![\w.])${escaped}(?![\w.])`).test(addedText)) used.add(key);
  }
  return used;
}
