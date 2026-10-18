import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterAll, beforeAll, expect, test } from "vitest";

import { registrationPlan } from "../../../plugins/monitor/lib/register.mjs";

const home = mkdtempSync(path.join(tmpdir(), "monitor-register-"));
const bin = path.join(home, "bin");
const originalPath = process.env.PATH;
const launcher = path.join(bin, "monitor");
const cliPaths = Object.fromEntries(
  ["claude", "codex", "agy", "gemini", "opencode"].map((name) => [name, path.join(bin, name)]),
);

beforeAll(() => {
  mkdirSync(bin);
  for (const file of [launcher, ...Object.values(cliPaths)]) {
    writeFileSync(file, "#!/bin/sh\nexit 0\n");
    chmodSync(file, 0o700);
  }
  process.env.PATH = `${bin}${path.delimiter}${originalPath ?? ""}`;
});

afterAll(() => {
  if (originalPath === undefined) delete process.env.PATH;
  else process.env.PATH = originalPath;
  rmSync(home, { recursive: true, force: true });
});

const account = (provider, extra = {}) => ({ provider, command: cliPaths.claude, ...extra });
const config = (accounts = [account("claude")]) => ({ accounts });

function stepsFor(plan, target) {
  return plan.filter((step) => step.target === target);
}

test("plans the default Claude profile with its normal keychain environment and add command", () => {
  const [step] = stepsFor(
    registrationPlan(config(), launcher, { home }),
    "Claude Code (~/.claude)",
  );

  expect(step).toMatchObject({
    kind: "cli",
    bin: cliPaths.claude,
    args: ["mcp", "add", "--scope", "user", "monitor", "--", launcher, "mcp"],
    env: { CLAUDE_CONFIG_DIR: null },
  });
});

test("plans a named Claude profile at its resolved directory", () => {
  const profile = path.join(home, ".claude-personal");
  const accounts = [account("claude", { configDir: profile })];
  const plan = registrationPlan(config(accounts), launcher, { home });
  const [step] = stepsFor(plan, "Claude Code (~/.claude-personal)");

  expect(step).toMatchObject({
    kind: "cli",
    args: ["mcp", "add", "--scope", "user", "monitor", "--", launcher, "mcp"],
    env: { CLAUDE_CONFIG_DIR: profile },
  });
});

test("uses the enabled monitor plugin instead of adding a duplicate Claude server", () => {
  const pluginHome = path.join(home, "plugin-case");
  const profile = path.join(pluginHome, ".claude");
  mkdirSync(profile, { recursive: true });
  writeFileSync(
    path.join(profile, "settings.json"),
    JSON.stringify({ enabledPlugins: { "monitor@marketplace": true } }),
  );

  const plan = registrationPlan(config(), launcher, { home: pluginHome });
  const steps = stepsFor(plan, "Claude Code (~/.claude)");

  expect(steps).toHaveLength(1);
  expect(steps[0]).toMatchObject({ kind: "file", note: "provided by the monitor plugin" });
  expect(steps[0].apply()).toStrictEqual({
    changed: false,
    note: "provided by the monitor plugin",
  });
});

test("deduplicates Claude profiles and skips them when requested", () => {
  const profile = path.join(home, ".claude-personal");
  const accounts = [
    account("claude", { configDir: profile }),
    account("claude", { command: "/different/claude", configDir: profile }),
  ];
  const plan = registrationPlan(config(accounts), launcher, { home });
  const skippedPlan = registrationPlan(config(accounts), launcher, { home, skipClaude: true });

  expect(stepsFor(plan, "Claude Code (~/.claude-personal)")).toHaveLength(1);
  expect(plan.find((step) => step.target === "Claude Code (~/.claude-personal)").bin).toBe(
    cliPaths.claude,
  );
  expect(plan.some((step) => step.target.startsWith("Claude Code"))).toBe(true);
  expect(skippedPlan.some((step) => step.target.startsWith("Claude Code"))).toBe(false);
});

test("plans add and remove commands for each installed CLI", () => {
  const add = registrationPlan(config(), launcher, { home });
  const remove = registrationPlan(config(), launcher, { home, remove: true });

  expect(stepsFor(add, "Codex (CLI and app)")[0]).toMatchObject({
    bin: cliPaths.codex,
    args: ["mcp", "add", "monitor", "--", launcher, "mcp"],
  });
  expect(stepsFor(remove, "Codex (CLI and app)")[0]).toMatchObject({
    bin: cliPaths.codex,
    args: ["mcp", "remove", "monitor"],
  });
  expect(stepsFor(add, "Antigravity (agy)")[0]).toMatchObject({
    bin: cliPaths.agy,
    args: ["mcp", "add", "monitor", launcher, "mcp"],
  });
  expect(stepsFor(remove, "Antigravity (agy)")[0]).toMatchObject({
    bin: cliPaths.agy,
    args: ["mcp", "remove", "monitor"],
  });
  expect(stepsFor(add, "Gemini CLI")[0]).toMatchObject({
    bin: cliPaths.gemini,
    args: ["mcp", "add", "-s", "user", "monitor", launcher, "mcp"],
  });
  expect(stepsFor(remove, "Gemini CLI")[0]).toMatchObject({
    bin: cliPaths.gemini,
    args: ["mcp", "remove", "-s", "user", "monitor"],
  });
  expect(stepsFor(add, "Claude Code (~/.claude)")[0].args).toStrictEqual([
    "mcp",
    "add",
    "--scope",
    "user",
    "monitor",
    "--",
    launcher,
    "mcp",
  ]);
  expect(stepsFor(remove, "Claude Code (~/.claude)")[0].args).toStrictEqual([
    "mcp",
    "remove",
    "--scope",
    "user",
    "monitor",
  ]);
});

test("plans OpenCode registration in its config file", () => {
  const opencodeDir = path.join(home, ".config", "opencode");
  mkdirSync(opencodeDir, { recursive: true });

  const plan = registrationPlan(config(), launcher, { home });
  const [step] = stepsFor(plan, "OpenCode");

  expect(step.kind).toBe("file");
  expect(step.apply()).toStrictEqual({ changed: true });
  const savedConfig = JSON.parse(readFileSync(path.join(opencodeDir, "opencode.json"), "utf8"));
  expect(savedConfig).toMatchObject({
    mcp: { monitor: { type: "local", command: [launcher, "mcp"], enabled: true } },
  });
});
