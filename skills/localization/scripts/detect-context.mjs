#!/usr/bin/env node
// Prints the localization context for the current working directory:
// which repo, its catalogs, their placeholder syntax, and where each catalog's
// translations live.

import fs from "node:fs";
import { fileURLToPath } from "node:url";

import { detectRepo, localeFiles, sourceFile, translationsDir } from "./lib/adapters.mjs";

function main() {
  const context = detectRepo(process.cwd());

  if (context.repo === "unknown") {
    console.log("unknown");
    console.log(`reason: ${context.reason}`);
    console.log(
      "expected a .i18n-catalogs.json catalog configuration at the repository root (see the skill's catalog configuration reference)",
    );
    return;
  }

  console.log(context.repo);
  for (const catalog of context.catalogs) {
    const resolved = { ...catalog, root: context.root, repo: context.repo };
    console.log(`catalog: ${catalog.id} (${catalog.format}, ${catalog.syntax})`);

    let locales;
    try {
      // deepcode ignore: directory comes from the detected repository, not untrusted input; local run with the user's own permissions.
      locales = localeFiles(resolved);
    } catch (error) {
      console.log(`translations: ${translationsDir(resolved)} (unreadable: ${error.message})`);
      continue;
    }

    const source = sourceFile(resolved);
    const targets = locales.filter((f) => f !== source);
    console.log(`translations: ${catalog.dir}`);
    console.log(`source: ${source}`);
    if (catalog.sourceNotes) console.log(`source coverage: ${catalog.sourceNotes}`);
    console.log(`locales: ${targets.length} target + source`);
  }
  const exclusions = context.exclusions ?? [];
  for (const exclusion of exclusions) {
    const files = exclusion.files ? `/${exclusion.files}` : "";
    console.log(`excluded: ${exclusion.path}${files} — ${exclusion.reason}`);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}
