import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { roomsCommand } from "../../../plugins/orchestrate/bin/rooms.mjs";
import {
  dispatchMember,
  growRoom,
  listRooms,
  newRoom,
  observeRoom,
  recoverRoom,
} from "../../../plugins/orchestrate/lib/rooms.mjs";
import { parseTeam, readRoom } from "../../../plugins/orchestrate/lib/stooges-room.mjs";

const directories = [];
async function directory() {
  const dir = await mkdtemp(path.join(tmpdir(), "ork-rooms-"));
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

  it("clears the intent after a refusal that launched nothing, so the member can dispatch again", async () => {
    const { root, room, taskDir, task, run } = await fixture();
    const refuse = async (args) => {
      if (args[0] === "dispatch") throw new Error("implementation already dispatched");
      return run(args);
    };
    await expect(
      dispatchMember(room.id, "haiku-1", { root, taskDir, run: refuse, kind: "implementation" }),
    ).rejects.toThrow("already dispatched");
    const refused = (await readRoom(room.id, root)).agents.find((agent) => agent.id === "haiku-1");
    expect(refused.dispatch).toBeUndefined();
    expect(refused.status).toBe("queued");
    expect(refused.dispatchError).toBe("implementation already dispatched");
    // A different kind is no longer blocked by the stale intent.
    await dispatchMember(room.id, "haiku-1", { root, taskDir, run, kind: "investigation" });
    expect(task.runs).toHaveLength(1);
  });

  it("keeps the intent when the outcome of a failed dispatch is unknown", async () => {
    const { root, room, taskDir, run } = await fixture();
    const lost = async (args) => {
      if (args[0] === "dispatch") {
        await run(args);
        throw new Error("response lost");
      }
      return run(args);
    };
    await expect(dispatchMember(room.id, "haiku-1", { root, taskDir, run: lost })).rejects.toThrow(
      "response lost",
    );
    const kept = (await readRoom(room.id, root)).agents.find((agent) => agent.id === "haiku-1");
    expect(kept.dispatch.requestId).toBeTruthy();
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

describe("rooms under a team", () => {
  it("run only the team's tasks, pass the lead's generation and show the lead", async () => {
    const root = await directory();
    const cwd = await directory();
    const taskDir = await directory();
    const room = await newRoom(parseTeam("claude:haiku"), {
      id: "team-room",
      cwd,
      root,
      authority: "team",
    });
    const task = {
      schema: "agent-executor.task.v1",
      task_id: "t",
      repository: room.cwd,
      runs: [],
      decisions: [],
    };
    const calls = [];
    const run = async (args) => {
      calls.push(args);
      if (args[0] === "show") return structuredClone(task);
      const request = args[args.indexOf("--request-id") + 1];
      task.runs.push({
        run_id: "r1",
        request_id: request,
        status: "active",
        engine: "claude",
        transport: "executor",
      });
      return { run_id: "r1" };
    };
    await expect(dispatchMember(room.id, "haiku-1", { root, taskDir, run })).rejects.toThrow(
      "not under the room's team team",
    );
    task.authority = { team: "team" };
    const lead = async (team) => ({
      team,
      generation: 3,
      status: "active",
      lead: { session: "s" },
    });
    await dispatchMember(room.id, "haiku-1", {
      root,
      taskDir,
      run,
      leadSession: "s",
      leadGeneration: 3,
      authority: lead,
    });
    const dispatch = calls.find((args) => args[0] === "dispatch");
    expect(dispatch.slice(dispatch.indexOf("--lead-session"))).toEqual([
      "--lead-session",
      "s",
      "--lead-generation",
      "3",
    ]);
    expect((await observeRoom(room.id, { root, run, authority: lead })).lead.generation).toBe(3);
  });
});

describe("room CLI", () => {
  it("creates and lists rooms", async () => {
    const root = await directory();
    const cwd = await directory();
    let output = "";
    const stdout = {
      write: (text) => {
        output += text;
      },
    };
    await roomsCommand(
      ["create", "--id", "new", "--cwd", cwd, "--team", "claude:haiku", "--group", "Fixes"],
      { stdout, root },
    );
    expect(JSON.parse(output).group).toBe("Fixes");
    output = "";
    await roomsCommand(["list", "--json"], { stdout, root });
    expect(JSON.parse(output).rooms.map((room) => room.id)).toEqual(["new"]);
  });
});
