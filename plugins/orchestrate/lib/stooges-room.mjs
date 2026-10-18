import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

export const ROOM_SCHEMA = "agent-executor.room.v1";
const ID = /^[\w-]{1,80}$/u;
const ENGINES = new Set(["claude", "agy", "codex", "opencode"]);
const EFFORTS = new Set(["low", "medium", "high", "xhigh", "max", "ultra"]);

export function mailboxRoot(env = process.env) {
  const expand = (value) => value.replace(/^~(?=\/|$)/u, () => homedir());
  const cache = expand(env.XDG_CACHE_HOME || `${homedir()}/.cache`);
  const base = env.AGENT_EXECUTOR_HOME
    ? expand(env.AGENT_EXECUTOR_HOME)
    : path.join(cache, "agent-executor");
  return path.join(base, "mailbox-v1");
}

export function roomPath(id, root = mailboxRoot()) {
  if (typeof id !== "string" || !ID.test(id))
    throw new Error("Room ID must contain 1–80 letters, digits, underscores or hyphens");
  return path.join(root, "rooms", `${id}.json`);
}

export async function loadPresets() {
  return JSON.parse(
    await readFile(new URL("../references/swarm-presets.json", import.meta.url), "utf8"),
  );
}

/** Team shorthand is resolved by the runner's live catalog at launch, never pinned here. */
export function parseTeam(text) {
  if (typeof text !== "string" || !text.trim()) throw new Error("A nonempty team is required");
  const members = [];
  for (const raw of text.split(",")) {
    const match = /^(claude|agy|codex|opencode):([^*,\s]+?)(?:\*(\d+))?$/u.exec(raw.trim());
    if (!match) throw new Error(`Invalid team member: ${raw}`);
    const [, engine, selection, copies = "1"] = match;
    const count = Number(copies);
    if (count < 1 || count > 64 || members.length + count > 64)
      throw new Error("A room supports 1–64 members");
    const parts = selection.split(":");
    if (parts.some((part) => !part)) throw new Error(`Invalid team member: ${raw}`);
    const effort = parts.length > 1 && EFFORTS.has(parts.at(-1)) ? parts.pop() : undefined;
    const tier = parts.join(":");
    let model = tier;
    if (engine === "codex" && ["luna", "sol", "terra", "astra"].includes(tier))
      model = `gpt:${tier}`;
    if (engine === "agy" && ["flash", "pro"].includes(tier))
      model = tier === "flash" ? "gemini:fast" : "gemini:pro";
    for (let i = 0; i < count; i++)
      members.push({ engine, model, ...(effort && { effort }), label: tier });
  }
  return members;
}

export function validateRoom(room) {
  if (
    !room ||
    room.schema !== ROOM_SCHEMA ||
    typeof room.id !== "string" ||
    !ID.test(room.id) ||
    !Array.isArray(room.agents)
  )
    throw new Error("Invalid swarm room: expected agent-executor.room.v1, id and agents");
  if (room.agents.length > 64) throw new Error("A room supports at most 64 members");
  const ids = new Set();
  for (const agent of room.agents) {
    if (
      !agent ||
      typeof agent.id !== "string" ||
      !ID.test(agent.id) ||
      ids.has(agent.id) ||
      !ENGINES.has(agent.engine) ||
      typeof agent.model !== "string"
    )
      throw new Error("Invalid or duplicate room member");
    ids.add(agent.id);
  }
  return room;
}

export function addMembers(room, members) {
  if (room.agents.length + members.length > 64)
    throw new Error("A room supports at most 64 members");
  for (const member of members) {
    let index = 1;
    const stem = member.label.replaceAll(/[^\w-]/gu, "-").slice(0, 60);
    while (room.agents.some((agent) => agent.id === `${stem}-${index}`)) index++;
    room.agents.push({ ...member, id: `${stem}-${index}`, status: "queued", output: [] });
  }
  return room;
}

export function createRoom(
  members,
  { id = randomUUID(), cwd = process.cwd(), now = Date.now() } = {},
) {
  const room = addMembers(
    {
      schema: ROOM_SCHEMA,
      id,
      cwd: path.resolve(cwd),
      createdAt: now,
      updatedAt: now,
      conductor: { status: "idle" },
      agents: [],
      messages: [],
      accounts: [],
    },
    members,
  );
  return validateRoom(room);
}

export async function readRoom(id, root) {
  const text = await readFile(roomPath(id, root), "utf8");
  const room = validateRoom(JSON.parse(text));
  if (room.id !== id) throw new Error("Room identity does not match its filename");
  return room;
}

/** Atomic replacement under a bounded, exclusive lock prevents concurrent spawns losing members. */
export async function updateRoom(id, update, { root = mailboxRoot(), create = false } = {}) {
  const file = roomPath(id, root);
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const lock = `${file}.lock`;
  const deadline = Date.now() + 3000;
  for (;;) {
    try {
      await mkdir(lock);
      break;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      if (Date.now() >= deadline) throw new Error(`Room is locked: ${id}`, { cause: error });
      await sleep(25);
    }
  }
  const temporary = `${file}.${randomUUID()}.tmp`;
  try {
    let previous;
    try {
      previous = await readRoom(id, root);
    } catch (error) {
      if (!create || error.code !== "ENOENT") throw error;
    }
    if (create && previous) throw new Error(`Room already exists: ${id}`);
    const room = validateRoom(await update(previous));
    if (room.id !== id) throw new Error("Cannot change room identity");
    room.updatedAt = Date.now();
    await writeFile(temporary, `${JSON.stringify(room, null, 2)}\n`, { mode: 0o600, flag: "wx" });
    await rename(temporary, file);
    return room;
  } finally {
    await rm(temporary, { force: true });
    await rm(lock, { recursive: true, force: true });
  }
}

export function coalesceMessages(messages = []) {
  const result = [];
  for (const message of messages) {
    const text = typeof message === "string" ? message : message.text;
    const from = typeof message === "string" ? "peer-mailbox" : message.from || "peer-mailbox";
    const last = result.at(-1);
    if (last?.text === text && last.from === from) last.count++;
    else result.push({ from, text, count: 1 });
  }
  return result;
}
