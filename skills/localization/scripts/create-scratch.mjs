#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

export function createScratch({ cwd = process.cwd(), temporaryRoot = os.tmpdir() } = {}) {
  let repoRoot;
  try {
    repoRoot = fs.realpathSync(
      execFileSync("git", ["rev-parse", "--show-toplevel"], {
        cwd,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
      }).trim(),
    );
  } catch (error) {
    throw new Error(
      "Cannot locate the consumer Git repository. Run from its checkout and verify Git with git --version.",
      { cause: error },
    );
  }

  let temporaryDirectory;
  try {
    temporaryDirectory = fs.realpathSync(path.resolve(temporaryRoot));
    if (!fs.statSync(temporaryDirectory).isDirectory()) throw new Error("not a directory");
  } catch (error) {
    throw new Error(
      `Cannot open temporary folder ${temporaryRoot}. Choose an existing external folder with --temp-dir.`,
      { cause: error },
    );
  }

  const relative = path.relative(repoRoot, temporaryDirectory);
  if (!path.isAbsolute(relative) && relative.split(path.sep)[0] !== "..") {
    throw new Error(
      `Temporary folder ${temporaryDirectory} is inside the consumer repository. Choose an external folder with --temp-dir.`,
    );
  }
  return fs.mkdtempSync(path.join(temporaryDirectory, "localization."));
}

function main() {
  try {
    const { values } = parseArgs({
      options: { "temp-dir": { type: "string" }, help: { type: "boolean" } },
    });
    if (values.help) {
      console.log('Usage: node create-scratch.mjs [--temp-dir "<external folder>"]');
      return;
    }
    console.log(createScratch({ temporaryRoot: values["temp-dir"] }));
  } catch (error) {
    console.error(`create-scratch: ${error.message}`);
    process.exitCode = 2;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  main();
}
