import { execFile } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { PassThrough } from "node:stream";
import { promisify } from "node:util";
import { afterEach, describe, expect, it } from "vitest";

import { herdrCommand } from "../../../plugins/orchestrate/bin/herdr.mjs";
import { roomsCommand } from "../../../plugins/orchestrate/bin/rooms.mjs";
import { herdrClient } from "../../../plugins/orchestrate/lib/herdr-client.mjs";
import { matchSession, swarmAdapter } from "../../../plugins/orchestrate/lib/herdr-swarms.mjs";
import {
  bindSession,
  dispatchMember,
  growRoom,
  listRooms,
  newRoom,
  observeRoom,
  recoverRoom,
} from "../../../plugins/orchestrate/lib/rooms.mjs";
import { parseTeam, readRoom } from "../../../plugins/orchestrate/lib/stooges-room.mjs";
import { overviewLines, runOverview } from "../../../plugins/orchestrate/lib/swarm-overview.mjs";

const directories = [];
const execute = promisify(execFile);
async function directory() {
  const dir = await mkdtemp(path.join(tmpdir(), "ork-herdr-"));
  directories.push(dir);
  return dir;
}
afterEach(async () => {
  for (const dir of directories.splice(0)) await rm(dir, { recursive: true, force: true });
});

async function fixture() {
  const root = await directory();
  const cwd = await directory();
  const taskDir = await directory();
  const room = await newRoom(parseTeam("claude:haiku,codex:luna:max"), {
    id: "room-a",
    cwd,
    root,
    group: "Research",
    title: "Investigate cache",
  });
  const task = {
    schema: "agent-executor.task.v1",
    task_id: "task-a",
    repository: room.cwd,
    runs: [],
    decisions: [],
    updated_at: "2026-10-20T15:00:00Z",
  };
  const run = async (args) => {
    if (args[0] === "show") return structuredClone(task);
    if (args[0] !== "dispatch") throw new Error(`Unexpected command ${args[0]}`);
    const request = args[args.indexOf("--request-id") + 1];
    let existing = task.runs.find((entry) => entry.request_id === request);
    if (!existing) {
      existing = {
        run_id: `run-${task.runs.length + 1}`,
        request_id: request,
        status: "active",
        transport: args[args.indexOf("--transport") + 1],
        engine: args[args.indexOf("--engine") + 1],
        result_path: path.join(taskDir, "result.json"),
      };
      task.runs.push(existing);
    }
    return structuredClone(existing);
  };
  return { root, cwd: room.cwd, taskDir, room, task, run };
}

function fakeHerdr() {
  const live = { panes: [], tabs: [], workspaces: [], agents: [] };
  const effects = [];
  let number = 0;
  let isLoseNextPaneReply = false;
  const rpc = async (method, params) => {
    effects.push({ method, params });
    if (method === "session.snapshot") return { snapshot: structuredClone(live) };
    if (method === "workspace.create") {
      const id = ++number;
      const workspace = { workspace_id: `workspace-${id}`, tokens: {}, label: params.label };
      const tab = { tab_id: `tab-${id}`, workspace_id: workspace.workspace_id };
      const root_pane = {
        pane_id: `pane-${id}`,
        terminal_id: `terminal-${id}`,
        workspace_id: workspace.workspace_id,
        tab_id: tab.tab_id,
        tokens: {},
      };
      live.workspaces.push(workspace);
      live.tabs.push(tab);
      live.panes.push(root_pane);
      return { workspace, tab, root_pane };
    }
    if (method === "tab.create") {
      const id = ++number;
      const tab = { tab_id: `tab-${id}`, workspace_id: params.workspace_id };
      const root_pane = {
        pane_id: `pane-${id}`,
        terminal_id: `terminal-${id}`,
        workspace_id: params.workspace_id,
        tab_id: tab.tab_id,
        tokens: {},
      };
      live.tabs.push(tab);
      live.panes.push(root_pane);
      return { tab, root_pane };
    }
    if (method === "plugin.pane.open") {
      const target = live.panes.find((pane) => pane.pane_id === params.target_pane_id);
      const id = ++number;
      const pane = { ...target, pane_id: `pane-${id}`, terminal_id: `terminal-${id}`, tokens: {} };
      live.panes.push(pane);
      if (isLoseNextPaneReply) throw new Error("Connection lost after creation");
      return { plugin_pane: { pane, plugin_id: "orkest.swarms", entrypoint: "room" } };
    }
    if (method === "pane.report_metadata") {
      Object.assign(
        live.panes.find((pane) => pane.pane_id === params.pane_id).tokens,
        params.tokens,
      );
      return {};
    }
    if (method === "workspace.report_metadata") {
      Object.assign(
        live.workspaces.find((workspace) => workspace.workspace_id === params.workspace_id).tokens,
        params.tokens,
      );
      return {};
    }
    if (["pane.focus", "agent.view.set", "agent.view.clear"].includes(method)) return { ok: true };
    throw new Error(`Unexpected method ${method}`);
  };
  return {
    live,
    effects,
    rpc,
    loseReply: () => {
      isLoseNextPaneReply = true;
    },
  };
}

describe("room execution", () => {
  it("creates and lists grouped rooms without dispatching, then preserves members when adding", async () => {
    const { root, room } = await fixture();
    await growRoom(room.id, parseTeam("agy:flash"), root);
    const result = await listRooms(root);
    expect(
      result.rooms.map((entry) => [
        entry.group,
        entry.title,
        entry.agents.map((member) => member.status),
      ]),
    ).toEqual([["Research", "Investigate cache", ["queued", "queued", "queued"]]]);
  });

  it("persists intent before launch and reuses it after a lost dispatch response", async () => {
    const { root, room, taskDir, task, run } = await fixture();
    let isLoseReply = true;
    const unreliable = async (args) => {
      const result = await run(args);
      if (args[0] === "dispatch") {
        const saved = await readRoom(room.id, root);
        expect(saved.agents[0].dispatch.requestId).toBe(result.request_id);
        if (isLoseReply) {
          isLoseReply = false;
          throw new Error("lost reply");
        }
      }
      return result;
    };
    await expect(
      dispatchMember(room.id, "haiku-1", { root, taskDir, run: unreliable }),
    ).rejects.toThrow("lost reply");
    const result = await dispatchMember(room.id, "haiku-1", { root, taskDir, run: unreliable });
    expect(result.room.agents[0].status).toBe("running");
    expect(result.dispatch.run_id).toBe("run-1");
    expect(task.runs.map((entry) => entry.run_id)).toEqual(["run-1"]);
  });

  it("shows finished work as awaiting review until the coordinator records a verdict", async () => {
    const { root, room, taskDir, task, run } = await fixture();
    await dispatchMember(room.id, "haiku-1", { root, taskDir, run });
    task.runs[0].status = "finished";
    task.runs[0].runner_status = "completed";
    expect((await observeRoom(room.id, { root, run })).agents[0].status).toBe("review");
    task.decisions.push({ run_id: "run-1", decision: "supported" });
    expect((await observeRoom(room.id, { root, run })).agents[0].status).toBe("completed");
    task.task_id = "replacement";
    expect((await observeRoom(room.id, { root, run })).agents[0].status).toBe("unknown");
  });

  it("reserves native work without inventing an interactive session", async () => {
    const { root, room, taskDir, task, run } = await fixture();
    const result = await dispatchMember(room.id, "luna-1", {
      root,
      taskDir,
      run,
      transport: "native",
    });
    expect(result.dispatch.transport).toBe("native");
    task.runs[0].status = "reserved";
    expect((await observeRoom(room.id, { root, run })).agents[1].status).toBe("reserved");
    expect(matchSession(room, result.room.agents[1], []).status).toBe("unbound");
  });

  it("rejects a changed task or request instead of dispatching another worker", async () => {
    const { root, room, taskDir, task, run } = await fixture();
    await dispatchMember(room.id, "haiku-1", { root, taskDir, run, requestId: "one" });
    await expect(
      dispatchMember(room.id, "haiku-1", { root, taskDir, run, requestId: "two" }),
    ).rejects.toThrow("already has a dispatch");
    await expect(
      dispatchMember(room.id, "luna-1", { root, taskDir, run, requestId: "one" }),
    ).rejects.toThrow("another dispatch");
    expect(task.runs.map((entry) => entry.request_id)).toEqual(["one"]);
  });

  it("reports corrupt rooms alongside valid rooms", async () => {
    const { root } = await fixture();
    await writeFile(path.join(root, "rooms", "broken.json"), "{", "utf8");
    const result = await listRooms(root);
    expect(result.rooms.map((room) => room.id)).toEqual(["room-a"]);
    expect(result.errors[0].file).toBe("broken.json");
  });

  it("reads the real coordinator task snapshot without launching a model", async () => {
    const { root, room, taskDir, task, run } = await fixture();
    await dispatchMember(room.id, "haiku-1", { root, taskDir, run });
    task.runs[0].status = "finished";
    task.runs[0].runner_status = "completed";
    await writeFile(path.join(taskDir, "task.json"), JSON.stringify(task));
    const observed = await observeRoom(room.id, { root });
    expect(observed.agents[0].runId).toBe("run-1");
    expect(observed.agents[0].status).toBe("review");
  });

  it("collects results only while the bound task identity still matches", async () => {
    const { root, room, taskDir, task, run } = await fixture();
    await dispatchMember(room.id, "haiku-1", { root, taskDir, run });
    const collect = async (args) => {
      if (args[0] === "recover") {
        task.runs[0].status = "finished";
        task.runs[0].runner_status = "completed";
        return task;
      }
      return run(args);
    };
    expect((await recoverRoom(room.id, { root, run: collect })).agents[0].status).toBe("review");
    task.task_id = "another-task";
    await expect(recoverRoom(room.id, { root, run: collect })).rejects.toThrow(
      "Task identity changed",
    );
  });
});

describe("Herdr grouping and session identity", () => {
  it("reuses a room view and groups two swarms in separate tabs within one workspace", async () => {
    const { root, cwd } = await fixture();
    await newRoom(parseTeam("agy:flash"), { root, cwd, id: "room-b", group: "Research" });
    const fake = fakeHerdr();
    const adapter = swarmAdapter({ root, socket: "test", rpc: fake.rpc });
    const first = await adapter.open("room-a");
    const again = await adapter.open("room-a");
    await adapter.open("room-b");
    expect(again).toEqual({ ...first, reused: true });
    expect(fake.live.workspaces.map((workspace) => workspace.label)).toEqual(["Research"]);
    expect(fake.live.tabs.map((tab) => tab.tab_id)).toEqual(["tab-1", "tab-3"]);
    expect(
      fake.effects
        .filter((effect) => effect.method === "plugin.pane.open")
        .map((effect) => effect.params.env),
    ).toEqual([{ ORK_ROOM_ID: "room-a" }, { ORK_ROOM_ID: "room-b" }]);
  });

  it("does not conflate equally named groups in different checkouts or server namespaces", async () => {
    const { root } = await fixture();
    await newRoom(parseTeam("agy:flash"), {
      root,
      cwd: await directory(),
      id: "room-b",
      group: "Research",
    });
    const fake = fakeHerdr();
    const adapter = swarmAdapter({ root, socket: "one", rpc: fake.rpc });
    await adapter.open("room-a");
    await adapter.open("room-b");
    expect(fake.live.workspaces.map((entry) => entry.workspace_id)).toEqual([
      "workspace-1",
      "workspace-3",
    ]);
    const another = fakeHerdr();
    await swarmAdapter({ root, socket: "two", rpc: another.rpc }).open("room-a");
    expect(another.live.workspaces.map((entry) => entry.workspace_id)).toEqual(["workspace-1"]);
  });

  it("does not repeat a layout mutation when the reply was lost", async () => {
    const { root } = await fixture();
    const fake = fakeHerdr();
    fake.loseReply();
    const adapter = swarmAdapter({ root, socket: "test", rpc: fake.rpc });
    await expect(adapter.open("room-a")).rejects.toThrow("Connection lost");
    await expect(adapter.open("room-a")).rejects.toThrow("uncertain");
    expect(fake.live.panes.map((pane) => pane.pane_id)).toEqual(["pane-1", "pane-2"]);
  });

  it("reattaches a moved detail pane after recreating the adapter", async () => {
    const { root } = await fixture();
    const fake = fakeHerdr();
    await swarmAdapter({ root, socket: "test", rpc: fake.rpc }).open("room-a");
    fake.live.panes[1].pane_id = "moved-detail";
    const reopened = await swarmAdapter({ root, socket: "test", rpc: fake.rpc }).open("room-a");
    expect(reopened).toEqual({ roomId: "room-a", paneId: "moved-detail", reused: true });
    expect(fake.live.panes.map((pane) => pane.pane_id)).toEqual(["pane-1", "moved-detail"]);
  });

  it("refuses a reused pane ID and requires explicit layout recovery", async () => {
    const { root } = await fixture();
    const fake = fakeHerdr();
    const adapter = swarmAdapter({ root, socket: "test", rpc: fake.rpc });
    await adapter.open("room-a");
    fake.live.panes[1] = { ...fake.live.panes[1], terminal_id: "unrelated", tokens: {} };
    await expect(adapter.open("room-a")).rejects.toThrow("missing or uncertain");
    expect(await adapter.forget("room-a")).toEqual({ forgotten: "room-a", workersStopped: false });
    expect((await adapter.open("room-a")).reused).toBe(false);
    expect(fake.live.panes.find((pane) => pane.terminal_id === "unrelated").pane_id).toBe("pane-2");
  });

  it("restores group metadata without making a duplicate workspace", async () => {
    const { root, cwd } = await fixture();
    const fake = fakeHerdr();
    const adapter = swarmAdapter({ root, socket: "test", rpc: fake.rpc });
    await adapter.open("room-a");
    fake.live.workspaces[0].tokens = {};
    await newRoom(parseTeam("claude:haiku"), { root, cwd, id: "room-b", group: "Research" });
    await adapter.open("room-b");
    expect(fake.live.workspaces.map((entry) => entry.workspace_id)).toEqual(["workspace-1"]);
    expect(fake.live.tabs.map((entry) => entry.tab_id)).toEqual(["tab-1", "tab-3"]);
  });

  it("serializes simultaneous layout opens so they cannot create duplicate views", async () => {
    const { root } = await fixture();
    const fake = fakeHerdr();
    const adapter = swarmAdapter({ root, socket: "test", rpc: fake.rpc });
    const results = await Promise.allSettled([adapter.open("room-a"), adapter.open("room-a")]);
    expect(
      results
        .filter((result) => result.status === "fulfilled")
        .map((result) => result.value.roomId),
    ).toEqual(["room-a"]);
    expect(fake.live.panes.map((pane) => pane.pane_id)).toEqual(["pane-1", "pane-2"]);
    expect((await adapter.open("room-a")).reused).toBe(true);
  });

  it("scopes sidebar filtering to the room and clears only its own projection", async () => {
    const { root } = await fixture();
    const fake = fakeHerdr();
    const adapter = swarmAdapter({ root, socket: "test", rpc: fake.rpc });
    expect(await adapter.filter("room-a")).toEqual({ ok: true });
    await adapter.clearFilter();
    expect(fake.effects.map((effect) => [effect.method, effect.params])).toEqual([
      [
        "agent.view.set",
        {
          source: "plugin:orkest.swarms",
          label: "Orkest swarm",
          filter: { op: "eq", field: { token: "ork_room_id" }, value: "room-a" },
          sort: [{ field: "attention", order: "desc" }],
        },
      ],
      ["agent.view.clear", { source: "plugin:orkest.swarms" }],
    ]);
  });

  it("follows a bound session across moved pane IDs but refuses ambiguity and other checkouts", async () => {
    const { root, room, cwd } = await fixture();
    await bindSession(room.id, "conductor", { root, session: "session-a", engine: "codex" });
    const fake = fakeHerdr();
    const pane = {
      pane_id: "moved-pane",
      terminal_id: "terminal-a",
      cwd,
      agent_session: { kind: "id", value: "session-a", agent: "codex" },
      tokens: {},
    };
    fake.live.panes.push(pane);
    const adapter = swarmAdapter({ root, socket: "test", rpc: fake.rpc });
    expect(await adapter.focus(room.id, "conductor")).toEqual({ paneId: "moved-pane" });
    fake.live.panes.push({ ...pane, pane_id: "another" });
    await expect(adapter.focus(room.id, "conductor")).rejects.toThrow("ambiguous");
    fake.live.panes.splice(1);
    pane.cwd = "/another-checkout";
    await expect(adapter.focus(room.id, "conductor")).rejects.toThrow("unbound");
  });

  it("keeps terminal idle separate from task verdict and emits display metadata only", async () => {
    const { root, room, cwd } = await fixture();
    await bindSession(room.id, "haiku-1", { root, session: "session-a", engine: "claude" });
    const fake = fakeHerdr();
    fake.live.panes.push({
      pane_id: "interactive",
      cwd,
      agent_status: "idle",
      agent_session: { kind: "id", value: "session-a", agent: "claude" },
      tokens: {},
    });
    const result = await swarmAdapter({ root, socket: "test", rpc: fake.rpc }).reconcile();
    expect(result.rooms[0].agents[0].status).toBe("queued");
    expect(fake.live.panes[0].tokens.ork_verdict).toBe("queued");
    expect(fake.effects.map((effect) => effect.method)).toEqual([
      "session.snapshot",
      "pane.report_metadata",
    ]);
  });
});

describe("public commands and terminal view", () => {
  it("offers help outside Herdr and supports creation/listing through the room CLI", async () => {
    const root = await directory();
    const cwd = await directory();
    let output = "";
    const stdout = {
      write: (text) => {
        output += text;
      },
    };
    await herdrCommand(["--help"], { stdout });
    expect(output).toContain("ork herdr overview");
    output = "";
    await roomsCommand(
      ["create", "--id", "new", "--cwd", cwd, "--team", "claude:haiku", "--group", "Fixes"],
      { stdout, root },
    );
    expect(JSON.parse(output).group).toBe("Fixes");
    output = "";
    await roomsCommand(["list", "--json"], { stdout, root });
    expect(JSON.parse(output).rooms.map((room) => room.id)).toEqual(["new"]);
  });

  it("shows review and unknown states and strips terminal control text", () => {
    const lines = overviewLines(
      {
        rooms: [
          {
            id: "one",
            group: "Research",
            cwd: "/repo",
            title: "\u{1B}[31mCache",
            agents: [{ status: "review" }, { status: "unknown" }],
          },
        ],
      },
      0,
      120,
      20,
    ).join("\n");
    expect(lines).toContain("> Cache · 1 review · 1 unknown");
    expect(lines.includes("\u{1B}")).toBe(false);
  });

  it("keeps the selected swarm visible when each row belongs to a different group", () => {
    const rooms = Array.from({ length: 20 }, (_, index) => ({
      id: `room-${index}`,
      group: `group-${index}`,
      cwd: "/repo",
      agents: [{ status: "queued" }],
    }));
    expect(overviewLines({ rooms }, 19, 100, 10).join("\n")).toContain("> room-19");
  });

  it("runs every manifest pane entrypoint from the packaged plugin root", async () => {
    const { stdout } = await execute("python3", [
      "-c",
      "import json,tomllib; print(json.dumps(tomllib.load(open('herdr-plugin.toml','rb'))['panes']))",
    ]);
    const entries = JSON.parse(stdout);
    const outputs = [];
    for (const entry of entries) {
      const [program, ...args] = entry.command;
      const result = await execute(program, [...args, "--help"], {
        cwd: process.cwd(),
        env: { ...process.env, HERDR_SOCKET_PATH: "" },
      });
      outputs.push([entry.id, result.stdout.includes("ork herdr overview")]);
    }
    expect(outputs).toEqual([
      ["overview", true],
      ["room", true],
      ["form", true],
    ]);
  });

  it("restores terminal state on close without stopping workers", async () => {
    const input = new PassThrough();
    input.isTTY = true;
    input.isRaw = false;
    input.setRawMode = (value) => {
      input.isRaw = value;
    };
    input.resume = () => {};
    input.pause = () => {};
    const output = new PassThrough();
    output.isTTY = true;
    output.columns = 100;
    output.rows = 24;
    let text = "";
    output.write = (value) => {
      text += value;
    };
    const running = runOverview({
      input,
      output,
      load: async () => ({ rooms: [], errors: [] }),
      interval: 10,
    });
    await new Promise((resolve) => setImmediate(resolve));
    input.emit("keypress", "q", { name: "q" });
    await running;
    expect(text).toContain("No swarms. Press c to create one.");
    expect(text.endsWith("\u{1B}[?25h\u{1B}[?1049l")).toBe(true);
    expect(input.isRaw).toBe(false);
  });

  it("uses a real socket request/response with matching IDs", async () => {
    const dir = await directory();
    const socket = path.join(dir, "h.sock");
    const server = createServer((connection) => {
      connection.once("data", (bytes) => {
        const request = JSON.parse(bytes.toString());
        connection.end(
          `${JSON.stringify({ id: request.id, result: { received: request.method, pane: request.params.pane_id } })}\n`,
        );
      });
    });
    await new Promise((resolve) => server.listen(socket, resolve));
    try {
      expect(await herdrClient({ socket })("pane.focus", { pane_id: "specific-pane" })).toEqual({
        received: "pane.focus",
        pane: "specific-pane",
      });
    } finally {
      await new Promise((resolve) => server.close(resolve));
    }
  });
});
