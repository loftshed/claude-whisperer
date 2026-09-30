import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, test, vi } from "vitest";

import {
  commandsFor,
  parseStatus,
  pinnedRuff,
  tidy,
} from "../../../.claude/hooks/lint-changed.mjs";

const scratch = [];
afterEach(() => {
  for (const directory of scratch.splice(0)) fs.rmSync(directory, { force: true, recursive: true });
});

function repository(files) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "stop-hook-"));
  scratch.push(root);
  for (const [file, content] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.writeFileSync(path.join(root, file), content);
  }
  return root;
}

describe("parseStatus", () => {
  test("keeps staged, modified and untracked paths and drops deletions", () => {
    const output = [
      "M  staged.js",
      " M edited.md",
      "?? new.py",
      " D gone.js",
      "D  removed.js",
      "R  moved.js",
      "old.js",
      "",
    ].join("\0");
    expect(parseStatus(output)).toEqual({
      changed: ["staged.js", "edited.md", "new.py", "moved.js"],
      staged: ["staged.js", "moved.js"],
    });
  });

  test("reads a real repository", () => {
    const root = repository({ "a.js": "1\n", "b.js": "1\n" });
    const git = (...args) => execFileSync("git", args, { cwd: root, encoding: "utf8" });
    git("init", "--quiet");
    git(
      "-c",
      "user.name=t",
      "-c",
      "user.email=t@example.invalid",
      "-c",
      "commit.gpgsign=false",
      "-c",
      "core.hooksPath=/dev/null",
      "commit",
      "--quiet",
      "--allow-empty",
      "-m",
      "start",
    );
    git("add", "a.js");
    fs.writeFileSync(path.join(root, "c.js"), "1\n");
    const status = git("status", "--porcelain=v1", "-z", "--untracked-files=all");
    expect(parseStatus(status)).toEqual({ changed: ["a.js", "b.js", "c.js"], staged: ["a.js"] });
  });
});

describe("commandsFor", () => {
  test("lints JavaScript, formats everything and formats Python with the pinned ruff", () => {
    const commands = commandsFor(["a.mjs", "b.md", "c.py"], {
      root: "/repo",
      ruffVersion: "1.2.3",
    });
    expect(commands).toEqual([
      ["/repo/node_modules/.bin/eslint", ["--fix", "--no-warn-ignored", "a.mjs"]],
      [
        "/repo/node_modules/.bin/prettier",
        ["--write", "--ignore-unknown", "--log-level", "warn", "a.mjs", "b.md", "c.py"],
      ],
      ["uvx", ["ruff@1.2.3", "check", "--fix", "--quiet", "c.py"]],
      ["uvx", ["ruff@1.2.3", "format", "--quiet", "c.py"]],
    ]);
  });

  test("the ruff pin comes from scripts/check.sh", () => {
    expect(pinnedRuff(repository({ "scripts/check.sh": "#!/bin/sh\nRUFF_VERSION=4.5.6\n" }))).toBe(
      "4.5.6",
    );
    expect(pinnedRuff(repository({}))).toBeNull();
  });
});

describe("tidy", () => {
  const status = (stdout) => ({ status: 0, stdout });

  test("runs the tools and restages what was staged", () => {
    const root = repository({ "a.js": "", "b.md": "" });
    const run = vi.fn((command) =>
      command === "git" ? status("M  a.js\0 M b.md\0") : { status: 0 },
    );
    expect(tidy({ root, run })).toEqual({ code: 0, message: "Tidied 2 changed file(s)." });
    expect(run.mock.calls.at(-1).slice(0, 2)).toEqual(["git", ["add", "--", "a.js"]]);
  });

  test("a failing tool blocks the first stop but not the retry", () => {
    const root = repository({ "a.js": "" });
    const run = (command) => (command === "git" ? status("?? a.js\0") : { status: 1 });
    expect(tidy({ root, run }).code).toBe(2);
    expect(tidy({ isRetry: true, root, run }).code).toBe(0);
  });

  test("a machine without uv skips ruff instead of failing", () => {
    const root = repository({ "a.py": "", "scripts/check.sh": "RUFF_VERSION=1.0.0\n" });
    const run = (command) => {
      if (command === "git") return status("?? a.py\0");
      return command === "uvx" ? { error: { code: "ENOENT" } } : { status: 0 };
    };
    expect(tidy({ root, run }).code).toBe(0);
  });

  test("nothing to do when nothing changed", () => {
    expect(tidy({ root: repository({}), run: () => status("") })).toEqual({
      code: 0,
      message: "Nothing changed.",
    });
  });
});
