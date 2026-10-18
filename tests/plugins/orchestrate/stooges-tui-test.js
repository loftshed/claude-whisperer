import { execFile } from "node:child_process";
import { EventEmitter } from "node:events";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { afterEach, describe, expect, it, vi } from "vitest";

import { runTui } from "../../../plugins/orchestrate/bin/stooges-tui.mjs";
import {
  addMembers,
  createRoom,
  parseTeam,
  readRoom,
  roomPath,
  updateRoom,
  validateRoom,
} from "../../../plugins/orchestrate/lib/stooges-room.mjs";
import {
  displayWidth,
  glyphTier,
  progressBar,
  StoogesView,
  wrapText,
} from "../../../plugins/orchestrate/lib/stooges-view.mjs";

const exec = promisify(execFile);
const cli = path.resolve("plugins/orchestrate/bin/ork.mjs");
const temporary = [];
async function directory() {
  const dir = await mkdtemp(path.join(tmpdir(), "stooges-test-"));
  temporary.push(dir);
  return dir;
}
afterEach(async () => {
  await Promise.all(temporary.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
});

function fixture(team = "claude:haiku,agy:flash,codex:luna:max") {
  const room = createRoom(parseTeam(team), { id: "sample-room", now: 1000 });
  room.conductor.status = "running";
  room.agents[0].status = "running";
  room.agents[0].output = ["Inspecting source", "Found two candidates"];
  room.agents[1].status = "blocked";
  room.agents[1].question = "Which branch should I review?";
  room.agents[1].output = ["Waiting for branch"];
  room.messages = [
    { from: "haiku-1", text: "Source inspected" },
    { from: "haiku-1", text: "Source inspected" },
  ];
  return room;
}

describe("swarm rooms", () => {
  it("expands mixed-provider teams with effort and distinct member identities", () => {
    const room = createRoom(parseTeam("claude:haiku*2,agy:flash*2,codex:luna:max"));
    expect(
      room.agents.map(({ id, model, effort, status }) => ({ id, model, effort, status })),
    ).toEqual([
      { id: "haiku-1", model: "haiku", effort: undefined, status: "queued" },
      { id: "haiku-2", model: "haiku", effort: undefined, status: "queued" },
      { id: "flash-1", model: "gemini:fast", effort: undefined, status: "queued" },
      { id: "flash-2", model: "gemini:fast", effort: undefined, status: "queued" },
      { id: "luna-1", model: "gpt:luna", effort: "max", status: "queued" },
    ]);
  });

  it.each([
    "",
    "claude:haiku*0",
    "claude:haiku*65",
    "codex",
    "unknown:luna",
    "codex::",
    "agy:flash,",
  ])("rejects invalid team %s", (team) => {
    expect(() => parseTeam(team)).toThrow();
  });

  it("preserves concurrent additions and existing conductor output", async () => {
    const root = await directory();
    await updateRoom("sample-room", () => fixture(), { root, create: true });
    await Promise.all([
      updateRoom("sample-room", (room) => addMembers(room, parseTeam("claude:haiku")), { root }),
      updateRoom("sample-room", (room) => addMembers(room, parseTeam("claude:haiku")), { root }),
    ]);
    const room = await readRoom("sample-room", root);
    expect(room.agents.map((agent) => agent.id)).toEqual([
      "haiku-1",
      "flash-1",
      "luna-1",
      "haiku-2",
      "haiku-3",
    ]);
    expect(room.agents[0].output).toEqual(["Inspecting source", "Found two candidates"]);
    expect(room.conductor.status).toBe("running");
  });

  it("rejects path escapes and malformed members", () => {
    expect(roomPath("safe", "/cache")).toBe("/cache/rooms/safe.json");
    expect(() => roomPath("../escape", "/cache")).toThrow("Room ID");
    const room = fixture();
    room.agents[0].id = undefined;
    expect(() => validateRoom(room)).toThrow("Invalid or duplicate");
  });

  it("rejects a stored room whose identity differs from its filename", async () => {
    const root = await directory();
    await updateRoom("sample-room", () => fixture(), { root, create: true });
    await writeFile(
      roomPath("sample-room", root),
      JSON.stringify({ ...fixture(), id: "other-room" }),
    );
    await expect(readRoom("sample-room", root)).rejects.toThrow(/identity|match/iu);
  });

  it("leaves the old room intact when an update fails", async () => {
    const root = await directory();
    await updateRoom("sample-room", () => fixture(), { root, create: true });
    await expect(
      updateRoom("sample-room", (room) => ({ ...room, id: "other" }), { root }),
    ).rejects.toThrow("identity");
    expect((await readRoom("sample-room", root)).id).toBe("sample-room");
    await updateRoom("sample-room", (room) => addMembers(room, parseTeam("claude:haiku")), {
      root,
    });
    expect((await readRoom("sample-room", root)).agents.at(-1).id).toBe("haiku-2");
  });
});

describe("adaptive view", () => {
  it("uses three columns on a wide terminal and stacked rows when narrow", () => {
    const view = new StoogesView(fixture(), { now: () => 2000 });
    const wide = view.render(120);
    expect(wide.join("\n")).toContain("Engine / model");
    expect(wide.join("\n")).toContain("haiku-1");
    expect(wide.some((line) => line.includes("[?] help"))).toBe(true);
    const narrow = view.render(70);
    expect(narrow.join("\n")).toContain("Agent");
    expect(narrow.join("\n")).toContain("Showing 1–3 of 3 agents");
    expect(narrow.find((line) => line.includes("Rows "))).toContain("Rows 1–3/3");
    expect(narrow.join("\n")).toContain("Which branch should I review?");
    expect(narrow.join("\n")).toContain("haiku-1: Source inspected (×2)");
  });

  it("focuses and pins detail while new output arrives", () => {
    const room = fixture("claude:haiku*2,agy:flash*2");
    const view = new StoogesView(room, { now: () => 2000 });
    view.handleInput("2");
    view.handleInput("p");
    view.handleInput("1");
    expect(view.render(90).join("\n")).toContain("Pinned · haiku-2");
    const changed = structuredClone(room);
    changed.agents[1].output.push("Branch selected");
    view.setRoom(changed);
    expect(view.render(90).join("\n")).toContain("Branch selected");
    view.handleInput("2");
    view.handleInput("p");
    expect(view.render(90).join("\n")).toContain("Focus · haiku-2");
  });

  it("supports more than nine members with page-local number keys", () => {
    const view = new StoogesView(fixture("claude:haiku*12"));
    view.handleInput("n");
    view.handleInput("3");
    expect(view.render(80).join("\n")).toContain("Focus · haiku-12");
    expect(view.render(80).join("\n")).toContain("page 2/2");
  });

  it("keeps focus, detail and navigation visible on a short terminal", () => {
    const room = fixture("claude:haiku*9");
    room.agents[8].output = ["Ninth agent result"];
    const view = new StoogesView(room, { height: 18 });
    view.render(80);
    for (let index = 0; index < 8; index++) view.handleInput("\u{1B}[B");
    const lines = view.render(80);
    expect(lines.length).toBeLessThanOrEqual(18);
    expect(lines.join("\n")).toContain("Focus · haiku-9");
    expect(lines.join("\n")).toContain("Ninth agent result");
    expect(lines.join("\n")).toContain("q quit");
  });

  it("fits Unicode and strips injected terminal controls", () => {
    const room = fixture();
    room.agents[0].output = ["\u{1B}[2J界界 👩‍💻 safe\u{7}"];
    const lines = new StoogesView(room).render(32);
    expect(lines.every((line) => displayWidth(line) <= 32)).toBe(true);
    expect(lines.join("\n")).toContain("safe");
    expect(lines.join("\n")).not.toContain("\u{1B}");
  });

  it("frames responsive layouts within terminal width and uses tone-colored ANSI safely", () => {
    const room = fixture();
    room.agents[0].output = ["\u{1B}[31munsafe\u{1B}[0m 👩‍💻 界\nnext line"];
    const view = new StoogesView(room, { now: () => 2000, height: 28, color: true });
    for (const width of [40, 47, 48, 63, 64, 79, 80, 120]) {
      const lines = view.render(width);
      expect(lines.every((line) => displayWidth(line) <= width)).toBe(true);
      expect(lines.join("\n")).not.toContain("\u{1B}[31munsafe");
    }
    expect(view.render(80).join("\n")).toContain("\u{1B}[36m");
    expect(view.render(80).join("\n")).toContain("\u{1B}[36;7m");
    expect(view.render(80).join("\n")).toContain("next line");
  });

  it("offers explicit glyph tiers, locale-safe fallback, and eighth-step bars", () => {
    expect(glyphTier({ ORK_GLYPHS: "nerd", LANG: "C" })).toBe("nerd");
    expect(glyphTier({ TERM: "dumb", LANG: "en_US.UTF-8" })).toBe("ascii");
    expect(glyphTier({ TERM: "xterm-256color" })).toBe("ascii");
    expect(progressBar(0.125, 4, "unicode")).toBe("▌░░░");
    expect(displayWidth("\u{F0000}")).toBe(1);
    expect(wrapText("界", 1)).toEqual(["?"]);
    const ascii = new StoogesView(fixture(), { height: 24, tier: "ascii" }).render(40);
    expect(ascii.join("\n")).toContain("+ ork");
    expect(ascii.every((line) => displayWidth(line) <= 40)).toBe(true);
  });

  it("renders an empty room, key help and a coalesced log overlay", () => {
    const room = fixture();
    room.agents = [];
    const view = new StoogesView(room, { height: 24 });
    expect(view.render(80).join("\n")).toContain("No agents queued for this room");
    view.handleInput("?");
    expect(view.render(80).join("\n")).toContain("Key reference");
    view.handleInput("\u{1B}");
    view.handleInput("g");
    expect(view.render(80).join("\n")).toContain("haiku-1: Source inspected (×2)");
    expect(view.render(80).join("\n")).toContain("Room log");
  });

  it("renders quota pills from monitor account data", () => {
    const room = fixture();
    room.accounts = [
      {
        id: "test",
        short: "CX",
        provider: "codex",
        pools: [{ id: "default", label: "Codex", windowIds: ["week"] }],
        windows: [
          {
            id: "week",
            label: "Weekly",
            windowMins: 10_080,
            usedPct: 20,
            remainingPct: 80,
            resetsAt: 1000 + 86_400_000,
          },
        ],
      },
    ];
    expect(new StoogesView(room, { now: () => 1000 }).render(100).join("\n")).toContain(
      "CX Weekly",
    );
    expect(new StoogesView(room, { now: () => 1000 }).render(100).join("\n")).toContain("80% left");
  });
});

describe("ork CLI", () => {
  it("prints offline fixtures and JSON without creating a room", async () => {
    const dir = await directory();
    const file = path.join(dir, "fixture.json");
    await writeFile(file, JSON.stringify(fixture()));
    const env = {
      ...process.env,
      AGENT_EXECUTOR_HOME: dir,
      LANG: "en_US.UTF-8",
      NO_COLOR: "1",
      TERM: "xterm-256color",
    };
    const printed = await exec(
      process.execPath,
      [cli, "stooges", "--fixture", file, "--print", "--width", "70"],
      { env },
    );
    expect(printed.stdout).toContain("Conductor running · 3 agents");
    expect(printed.stdout).toContain("┌ ork · sample-room");
    expect(printed.stdout).toContain("[?] help");
    expect(printed.stdout).not.toContain("\u{1B}");
    const json = await exec(process.execPath, [cli, "stooges", "--fixture", file, "--json"], {
      env,
    });
    expect(JSON.parse(json.stdout).agents[0].output).toEqual([
      "Inspecting source",
      "Found two candidates",
    ]);
    await expect(
      readFile(path.join(dir, "mailbox-v1/rooms/sample-room.json")),
    ).rejects.toMatchObject({ code: "ENOENT" });
  });

  it.each([
    ["--width", "0"],
    ["--width", "Infinity"],
    ["--unknown"],
    ["--team", "claude:haiku", "--preset", "scout-swarm"],
    ["--print", "--json"],
    ["spawn", "--team", "claude:haiku"],
    ["--preset", "missing"],
  ])("rejects invalid options %j before creating rooms", async (...args) => {
    const dir = await directory();
    await expect(
      exec(process.execPath, [cli, "stooges", ...args], {
        env: { ...process.env, AGENT_EXECUTOR_HOME: dir },
      }),
    ).rejects.toMatchObject({ code: 1, stderr: expect.stringContaining("ork:") });
    await expect(readFile(path.join(dir, "mailbox-v1/rooms"))).rejects.toMatchObject({
      code: "ENOENT",
    });
  });

  it.each([
    "not json",
    "{}",
    JSON.stringify({ schema: "agent-executor.room.v1", id: "../escape", agents: [] }),
  ])("rejects invalid fixture %s without creating a room", async (contents) => {
    const dir = await directory();
    const file = path.join(dir, "fixture.json");
    await writeFile(file, contents);
    await expect(
      exec(process.execPath, [cli, "stooges", "--fixture", file, "--print"], {
        env: { ...process.env, AGENT_EXECUTOR_HOME: dir },
      }),
    ).rejects.toMatchObject({ code: 1, stderr: expect.stringContaining("ork:") });
  });

  it("creates a preset room and appends queued agents to the same durable room", async () => {
    const dir = await directory();
    const env = { ...process.env, AGENT_EXECUTOR_HOME: dir };
    const created = await exec(
      process.execPath,
      [cli, "stooges", "--preset", "scout-swarm", "--json"],
      { env },
    );
    const room = JSON.parse(created.stdout);
    expect(room.agents.map((agent) => agent.id)).toEqual([
      "haiku-1",
      "haiku-2",
      "flash-1",
      "flash-2",
      "luna-1",
    ]);
    const spawned = await exec(
      process.execPath,
      [cli, "stooges", "spawn", "--attach-room", room.id, "--team", "claude:haiku", "--json"],
      { env },
    );
    expect(JSON.parse(spawned.stdout).agents.at(-1)).toMatchObject({
      id: "haiku-3",
      status: "queued",
    });
    expect((await readRoom(room.id, path.join(dir, "mailbox-v1"))).agents).toHaveLength(6);
  });
});

describe("terminal lifecycle", () => {
  it.each([false, true])("restores terminal state after q when raw mode was %s", async (wasRaw) => {
    vi.useFakeTimers();
    // eslint-disable-next-line unicorn/prefer-event-target -- Node readline requires a stream EventEmitter API.
    const input = new EventEmitter();
    input.isTTY = true;
    input.isRaw = wasRaw;
    input.setRawMode = vi.fn((raw) => {
      input.isRaw = raw;
    });
    input.resume = vi.fn();
    input.pause = vi.fn();
    const output = { isTTY: true, rows: 24, columns: 80, write: vi.fn() };
    const signals = { SIGINT: process.listeners("SIGINT"), SIGTERM: process.listeners("SIGTERM") };
    const load = vi.fn(async () => fixture());
    let running;
    try {
      running = runTui(load, { input, output, quota: false });
      await vi.advanceTimersByTimeAsync(0);
      expect(input.isRaw).toBe(true);
      expect(output.write.mock.calls.flat().join("")).toContain("\u{1B}[?1049h\u{1B}[?25l");
      input.emit("keypress", "q", { name: "q" });
      await running;
      expect(input.isRaw).toBe(wasRaw);
      expect(input.pause).toHaveBeenCalledOnce();
      expect(input.listenerCount("keypress")).toBe(0);
      expect(input.listenerCount("end")).toBe(0);
      expect(process.listeners("SIGINT")).toEqual(signals.SIGINT);
      expect(process.listeners("SIGTERM")).toEqual(signals.SIGTERM);
      expect(output.write.mock.calls.flat().join("")).toContain("\u{1B}[?25h\u{1B}[?1049l");
      expect(vi.getTimerCount()).toBe(0);
      const frames = output.write.mock.calls.length;
      const loads = load.mock.calls.length;
      await vi.advanceTimersByTimeAsync(2000);
      expect(output.write).toHaveBeenCalledTimes(frames);
      expect(load).toHaveBeenCalledTimes(loads);
    } finally {
      input.emit("keypress", "q", { name: "q" });
      await running;
      vi.useRealTimers();
    }
  });

  it("does not write the same frame on every timer tick and redraws on resize", async () => {
    vi.useFakeTimers();
    // eslint-disable-next-line unicorn/prefer-event-target -- Readline keypress and terminal resize use Node stream events.
    const input = new EventEmitter();
    input.isTTY = true;
    input.isRaw = false;
    input.setRawMode = vi.fn();
    input.resume = vi.fn();
    input.pause = vi.fn();
    // eslint-disable-next-line unicorn/prefer-event-target -- Readline keypress and terminal resize use Node stream events.
    const output = new EventEmitter();
    output.isTTY = true;
    output.rows = 24;
    output.columns = 80;
    output.write = vi.fn();
    const running = runTui(
      async () => {
        const room = fixture();
        for (const agent of room.agents) agent.status = "completed";
        return room;
      },
      { input, output, quota: false },
    );
    try {
      await vi.advanceTimersByTimeAsync(0);
      const initialWrites = output.write.mock.calls.length;
      await vi.advanceTimersByTimeAsync(600);
      expect(output.write).toHaveBeenCalledTimes(initialWrites);
      output.columns = 90;
      output.emit("resize");
      expect(output.write).toHaveBeenCalledTimes(initialWrites + 1);
      input.emit("keypress", "q", { name: "q" });
      await running;
      expect(input.setRawMode).toHaveBeenLastCalledWith(false);
      expect(output.listenerCount("resize")).toBe(0);
    } finally {
      input.emit("keypress", "q", { name: "q" });
      await running;
      vi.useRealTimers();
    }
  });

  it("restores terminal state when painting fails after entering the alternate screen", async () => {
    vi.useFakeTimers();
    // eslint-disable-next-line unicorn/prefer-event-target -- Readline keypress uses the Node stream EventEmitter API.
    const input = new EventEmitter();
    input.isTTY = true;
    input.isRaw = false;
    input.setRawMode = vi.fn((raw) => {
      input.isRaw = raw;
    });
    input.resume = vi.fn();
    input.pause = vi.fn();
    const output = { isTTY: true, rows: 24, columns: 80, write: vi.fn() };
    output.write
      .mockImplementationOnce(() => {})
      .mockImplementationOnce(() => {
        throw new Error("terminal write failed");
      });
    try {
      await expect(runTui(async () => fixture(), { input, output, quota: false })).rejects.toThrow(
        "terminal write failed",
      );
      expect(input.isRaw).toBe(false);
      expect(input.pause).toHaveBeenCalledOnce();
      expect(output.write.mock.calls.at(-1)[0]).toContain("\u{1B}[?1049l");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("Pi room adapter", () => {
  async function adapter({ hasUI = true, mode = "tui" } = {}) {
    const dir = await directory();
    vi.stubEnv("AGENT_EXECUTOR_HOME", dir);
    const commands = new Map();
    const tools = new Map();
    const events = new Map();
    let component;
    const requestRender = vi.fn();
    const ctx = {
      cwd: dir,
      hasUI,
      mode,
      ui: {
        notify: vi.fn(),
        setWidget: vi.fn((name, factory) => {
          expect(name).toBe("stooges-room");
          component?.dispose();
          component = factory ? factory({ requestRender }, {}) : undefined;
        }),
      },
    };
    const { default: register } =
      await import("../../../plugins/orchestrate/lib/pi-stooges-widget.mjs");
    register({
      registerCommand: (name, command) => commands.set(name, command),
      registerTool: (tool) => tools.set(tool.name, tool),
      on: (name, handler) => events.set(name, handler),
    });
    return {
      ctx,
      root: path.join(dir, "mailbox-v1"),
      command: (args) => commands.get("stooges").handler(args, ctx),
      tool: tools.get("stooges_spawn"),
      shutdown: () => events.get("session_shutdown")({}, ctx),
      view: () => component,
      requestRender,
    };
  }

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
  });

  it("attaches a widget, follows durable output and cleans up on off", async () => {
    const app = await adapter();
    await updateRoom("sample-room", () => fixture(), { root: app.root, create: true });
    vi.useFakeTimers();
    try {
      await app.command("sample-room");
      expect(app.view().render(80).join("\n")).toContain("Inspecting source");
      await app.command("focus 2");
      await app.command("pin");
      expect(app.view().render(80).join("\n")).toContain("Pinned · flash-1");
      await updateRoom(
        "sample-room",
        (room) => {
          room.agents[1].output.push("Review complete");
          return room;
        },
        { root: app.root },
      );
      await vi.advanceTimersByTimeAsync(1000);
      await vi.waitFor(() => expect(app.view().render(80).join("\n")).toContain("Review complete"));
      expect(app.requestRender).toHaveBeenCalled();
      await app.command("off");
      expect(app.view()).toBeUndefined();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      await app.shutdown();
    }
  });

  it("creates rooms in the active directory and stops mounted timers at shutdown", async () => {
    const app = await adapter();
    vi.useFakeTimers();
    try {
      await app.command("new claude:haiku*2");
      const { readdir } = await import("node:fs/promises");
      const files = (await readdir(path.join(app.root, "rooms"))).filter((name) =>
        name.endsWith(".json"),
      );
      expect(files).toHaveLength(1);
      const room = await readRoom(files[0].slice(0, -5), app.root);
      expect(room.cwd).toBe(app.ctx.cwd);
      expect(room.agents).toHaveLength(2);
      expect(app.view().render(80).join("\n")).toContain("haiku-2");
      expect(vi.getTimerCount()).toBeGreaterThan(0);
      await app.shutdown();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      await app.shutdown();
    }
  });

  it("queues members headlessly and rejects aborted or invalid requests without mutation", async () => {
    const app = await adapter({ hasUI: false, mode: "rpc" });
    await updateRoom("sample-room", () => fixture(), { root: app.root, create: true });
    const parameters = app.tool.parameters;
    // eslint-disable-next-line unicorn/prefer-structured-clone -- Verify the tool schema survives JSON transport.
    expect(JSON.parse(JSON.stringify(parameters))).toMatchObject({
      type: "object",
      required: expect.arrayContaining(["roomId", "team"]),
    });
    const result = await app.tool.execute(
      "call-1",
      { roomId: "sample-room", team: "claude:haiku*2" },
      undefined,
      undefined,
      app.ctx,
    );
    expect(result.details).toMatchObject({
      roomId: "sample-room",
      agentIds: ["haiku-2", "haiku-3"],
    });
    expect(result.content[0]).toMatchObject({
      type: "text",
      text: expect.stringContaining("Queued"),
    });
    expect((await readRoom("sample-room", app.root)).agents).toHaveLength(5);
    for (const args of [
      { roomId: "sample-room" },
      { roomId: "../escape", team: "claude:haiku" },
      { roomId: "sample-room", team: 42 },
    ]) {
      await expect(
        app.tool.execute("invalid", args, undefined, undefined, app.ctx),
      ).rejects.toThrow();
    }
    const controller = new AbortController();
    controller.abort();
    await expect(
      app.tool.execute(
        "aborted",
        { roomId: "sample-room", team: "claude:haiku" },
        controller.signal,
        undefined,
        app.ctx,
      ),
    ).rejects.toThrow();
    expect((await readRoom("sample-room", app.root)).agents).toHaveLength(5);
    expect(app.ctx.ui.setWidget).not.toHaveBeenCalled();
  });
});
