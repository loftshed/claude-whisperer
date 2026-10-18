import { execFile } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { afterEach, describe, expect, it } from "vitest";

const exec = promisify(execFile);
const cli = path.resolve("plugins/orchestrate/bin/ork.mjs");
const temporary = [];
async function sandbox() {
  const dir = await mkdtemp(path.join(tmpdir(), "ork-cli-test-"));
  temporary.push(dir);
  const config = path.join(dir, "routes.json");
  const monitor = path.join(dir, "monitor.json");
  await writeFile(config, JSON.stringify({ routes: {} }));
  await writeFile(monitor, JSON.stringify({ accounts: [] }));
  const env = {
    ...process.env,
    AGENT_EXECUTOR_HOME: dir,
    AGENT_EXECUTOR_CONFIG: config,
    MONITOR_CONFIG: monitor,
    MONITOR_CACHE_DIR: path.join(dir, "monitor-cache"),
    NO_COLOR: "1",
  };
  return { dir, config, run: (args) => exec(process.execPath, [cli, ...args], { env }) };
}
afterEach(async () => {
  await Promise.all(temporary.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
});

describe("ork routes", () => {
  it("wraps strengths at spaces and splits only oversized words", async () => {
    const app = await sandbox();
    for (const [strength, expected] of [
      [
        "complex coding, agentic workflows, professional software work",
        ["complex coding, agentic workflows,", "professional software work"],
      ],
      ["x".repeat(50), ["x".repeat(36), "x".repeat(14)]],
    ]) {
      await writeFile(
        app.config,
        JSON.stringify({
          routes: { "debug-invariants": { engine: "codex", model: "gpt:sol" } },
          families: {
            gpt: {
              engine: "codex",
              tiers: { sol: "fixture" },
              tier_strengths: { sol: [strength] },
            },
          },
        }),
      );
      const { stdout } = await app.run([
        "routes",
        "--archetype",
        "debug-invariants",
        "--width",
        "110",
      ]);
      const cells = stdout
        .trimEnd()
        .split("\n")
        .slice(2)
        .map((line) => line.split(" | ").at(-1).trim());
      expect(cells).toEqual(expected);
    }
  });

  it("lists the six task archetypes with ordered candidates and tier strengths", async () => {
    const app = await sandbox();
    const result = JSON.parse((await app.run(["routes", "--json"])).stdout);
    expect(result.schema).toBe("ork.routes.v1");
    expect(
      result.routes.map((route) => route.archetype).sort((a, b) => a.localeCompare(b)),
    ).toEqual([
      "architecture-conductor",
      "debug-invariants",
      "independent-review",
      "mechanical-sweep",
      "structural-refactor",
      "ui-design-system",
    ]);
    for (const route of result.routes) {
      expect(route.candidates.length).toBeGreaterThan(0);
      for (const candidate of route.candidates) {
        expect(candidate).toMatchObject({
          engine: expect.any(String),
          model: expect.any(String),
          tier_strengths: expect.any(Array),
        });
      }
    }
  });

  it("filters an archetype and respects the configured primary candidate", async () => {
    const app = await sandbox();
    await writeFile(
      app.config,
      JSON.stringify({
        routes: {
          "debug-invariants": { engine: "claude", model: "claude:fast", effort: "medium" },
        },
      }),
    );
    const result = JSON.parse(
      (await app.run(["routes", "--archetype", "debug-invariants", "--json"])).stdout,
    );
    expect(result.routes).toHaveLength(1);
    expect(result.routes[0]).toMatchObject({
      archetype: "debug-invariants",
      candidates: [
        expect.objectContaining({ engine: "claude", model: "claude:fast", effort: "medium" }),
      ],
    });
    const printed = (await app.run(["routes", "--archetype", "debug-invariants", "--width", "80"]))
      .stdout;
    expect(printed).toContain("debug-invariants");
    expect(printed).toContain("claude:fast");
    expect(printed).not.toContain("\u{1B}");
  });
});

describe("ork telemetry", () => {
  it("summarizes local receipts and renders rates without running models", async () => {
    const app = await sandbox();
    const runs = path.join(app.dir, "runs with spaces");
    for (const [id, status, duration] of [
      ["one", "COMPLETE", 1000],
      ["two", "FAILED", 3000],
    ]) {
      const run = path.join(runs, id);
      await mkdir(run, { recursive: true });
      await writeFile(
        path.join(run, "result.json"),
        JSON.stringify({
          task_archetype: "debug-invariants",
          engine: "codex",
          model: "fixture-model",
          effort: "high",
          status,
          duration_ms: duration,
          verification: { passed: status === "COMPLETE" },
        }),
      );
    }
    const result = JSON.parse((await app.run(["telemetry", "--runs-dir", runs, "--json"])).stdout);
    expect(result).toMatchObject({
      runs: 2,
      skipped_results: 0,
      groups: [
        {
          task_archetype: "debug-invariants",
          engine: "codex",
          model: "fixture-model",
          runs: 2,
          accepted_rate: 0.5,
          first_pass_rate: 0.5,
          median_duration_ms: 2000,
        },
      ],
    });
    const printed = (await app.run(["telemetry", "--runs-dir", runs, "--width", "120"])).stdout;
    expect(printed).toContain("debug-invariants");
    expect(printed).toMatch(/50(?:\.0)?%/u);
    expect(printed).toContain("2.0s");
    expect(printed.split("\n").every((line) => line.length <= 120)).toBe(true);
    expect(printed).not.toContain("\u{1B}");
  });
});

describe("ork quota", () => {
  it("supports isolated JSON, line and refresh output with no configured providers", async () => {
    const app = await sandbox();
    const result = JSON.parse((await app.run(["quota", "--json"])).stdout);
    expect(result.accounts).toEqual([]);
    expect((await app.run(["quota", "--line"])).stdout.trim()).toBe("");
    expect((await app.run(["quota", "--refresh"])).stdout).toContain("Monitor");
  });
});

describe("ork command validation", () => {
  it.each([
    ["routes", "--runs-dir", "/tmp"],
    ["routes", "--refresh"],
    ["routes", "--archetype", "unknown"],
    ["routes", "--width", "0"],
    ["telemetry", "--team", "claude:haiku"],
    ["telemetry", "--width", "NaN"],
    ["quota", "--json", "--line"],
    ["quota", "--archetype", "debug-invariants"],
  ])("rejects unrelated or invalid command options %j", async (...args) => {
    const app = await sandbox();
    await expect(app.run(args)).rejects.toMatchObject({
      code: 1,
      stderr: expect.stringContaining("ork:"),
    });
  });
});
