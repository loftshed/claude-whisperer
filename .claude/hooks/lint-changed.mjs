#!/usr/bin/env node
// Claude Code Stop hook. Before a turn ends, tidy every file the working tree
// changed: ESLint --fix for JavaScript, Prettier for anything it understands,
// ruff for Python. Files that were staged are staged again afterwards so the
// fixes are not left behind. A tool that still fails blocks the first stop
// attempt (exit 2, the message goes back to Claude); the retry is let through.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

/**
 * Parse `git status --porcelain=v1 -z` into the paths that still exist and the
 * subset that has staged changes. Rename entries carry their old path as an
 * extra NUL-separated field, which is skipped.
 */
export function parseStatus(output) {
  const fields = output.split("\0");
  const changed = [];
  const staged = [];
  for (let index = 0; index < fields.length; index++) {
    const field = fields[index];
    if (field.length < 4) continue;
    const [indexState, treeState] = field;
    const file = field.slice(3);
    if (indexState === "R" || indexState === "C") index++;
    if (treeState === "D" || (indexState === "D" && treeState === " ")) continue;
    changed.push(file);
    if (indexState !== " " && indexState !== "?") staged.push(file);
  }
  return { changed, staged };
}

/** The commands to run for a set of changed files, in order. */
export function commandsFor(files, { root = ROOT, ruffVersion } = {}) {
  const bin = (name) => path.join(root, "node_modules", ".bin", name);
  const javascript = files.filter((file) => /\.(?:c|m)?jsx?$/u.test(file));
  const python = files.filter((file) => file.endsWith(".py"));
  const commands = [];
  if (javascript.length > 0) {
    commands.push([bin("eslint"), ["--fix", "--no-warn-ignored", ...javascript]]);
  }
  if (files.length > 0) {
    commands.push([
      bin("prettier"),
      ["--write", "--ignore-unknown", "--log-level", "warn", ...files],
    ]);
  }
  if (python.length > 0 && ruffVersion) {
    const ruff = `ruff@${ruffVersion}`;
    commands.push(["uvx", [ruff, "check", "--fix", "--quiet", ...python]]);
    commands.push(["uvx", [ruff, "format", "--quiet", ...python]]);
  }
  return commands;
}

/** The ruff release pinned by scripts/check.sh, so the hook and the gate agree. */
export function pinnedRuff(root = ROOT) {
  try {
    const script = fs.readFileSync(path.join(root, "scripts", "check.sh"), "utf8");
    return /^RUFF_VERSION=(\S+)/mu.exec(script)?.[1] ?? null;
  } catch {
    return null;
  }
}

export function tidy({ root = ROOT, run = spawnSync, isRetry = false } = {}) {
  const status = run("git", ["status", "--porcelain=v1", "-z", "--untracked-files=all"], {
    cwd: root,
    encoding: "utf8",
  });
  if (status.status !== 0) {
    return { code: isRetry ? 0 : 2, message: "Could not list changed files with git status." };
  }
  const { changed, staged } = parseStatus(status.stdout);
  const files = changed.filter((file) =>
    fs.statSync(path.join(root, file), { throwIfNoEntry: false })?.isFile(),
  );
  if (files.length === 0) return { code: 0, message: "Nothing changed." };

  const failed = [];
  const commands = commandsFor(files, { root, ruffVersion: pinnedRuff(root) });
  for (const [command, args] of commands) {
    const result = run(command, args, { cwd: root, stdio: "inherit" });
    // uv missing locally is not a failure; the CI gate still runs ruff.
    if (result.error?.code === "ENOENT" && command === "uvx") continue;
    if (result.status !== 0) failed.push(path.basename(command));
  }
  const restage = staged.filter((file) => files.includes(file));
  if (failed.length === 0 && restage.length > 0) {
    const added = run("git", ["add", "--", ...restage], { cwd: root, stdio: "inherit" });
    if (added.status !== 0) failed.push("git add");
  }
  if (failed.length === 0) return { code: 0, message: `Tidied ${files.length} changed file(s).` };
  return {
    code: isRetry ? 0 : 2,
    message: `${[...new Set(failed)].join(", ")} reported problems in the changed files; fix them before finishing.`,
  };
}

function isStopRetry() {
  if (process.stdin.isTTY) return false;
  try {
    return JSON.parse(fs.readFileSync(0, "utf8") || "{}").stop_hook_active === true;
  } catch {
    return false;
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  const { code, message } = tidy({ isRetry: isStopRetry() });
  (code === 0 ? console.log : console.error)(message);
  process.exitCode = code;
}
