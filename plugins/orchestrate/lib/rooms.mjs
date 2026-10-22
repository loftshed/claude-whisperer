import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { readdir, readFile, realpath } from "node:fs/promises";
import { hostname } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { addMembers, createRoom, mailboxRoot, readRoom, updateRoom } from "./stooges-room.mjs";

const execute = promisify(execFile);
const coordinatorPath = fileURLToPath(
  new URL("../skills/orchestrate/scripts/coordinator.py", import.meta.url),
);
const authorityPath = fileURLToPath(
  new URL("../skills/orchestrate/scripts/authority.py", import.meta.url),
);

/** The team's current lead and generation; the authority record is written only by authority.py. */
export async function authorityShow(team) {
  try {
    const { stdout } = await execute("python3", [authorityPath, "show", "--team", team], {
      maxBuffer: 8 * 1024 * 1024,
    });
    const { authority } = JSON.parse(stdout);
    return {
      team,
      generation: authority.generation,
      status: authority.status,
      lead: authority.lead,
      offer: authority.offer?.to,
    };
  } catch (error) {
    return { team, error: error.stderr?.trim() || error.message };
  }
}

/** Coordinator arguments naming the caller as the team's lead at a generation. */
function leadArgs(options) {
  if (!options.leadSession) return [];
  return [
    "--lead-session",
    options.leadSession,
    "--lead-generation",
    String(options.leadGeneration),
  ];
}

export async function coordinator(args) {
  try {
    const { stdout } = await execute("python3", [coordinatorPath, ...args], {
      maxBuffer: 8 * 1024 * 1024,
    });
    return JSON.parse(stdout);
  } catch (error) {
    throw new Error(error.stderr?.trim() || error.message, { cause: error });
  }
}

export async function listRooms(root = mailboxRoot()) {
  let files;
  try {
    files = await readdir(path.join(root, "rooms"));
  } catch (error) {
    if (error.code === "ENOENT") return { rooms: [], errors: [] };
    throw error;
  }
  const rooms = [];
  const errors = [];
  files.sort((a, b) => a.localeCompare(b));
  for (const file of files) {
    if (!file.endsWith(".json")) continue;
    try {
      rooms.push(await readRoom(file.slice(0, -5), root));
    } catch (error) {
      errors.push({ file, error: error.message });
    }
  }
  return { rooms, errors };
}

export async function newRoom(
  members,
  { id = randomUUID(), cwd, group, title, authority, root = mailboxRoot() } = {},
) {
  const room = createRoom(members, { id, cwd: await realpath(cwd || process.cwd()) });
  room.group = group || path.basename(room.cwd);
  room.title = title || room.id;
  room.machine = hostname();
  if (authority) room.authority = authority;
  await updateRoom(id, () => room, { root, create: true });
  return room;
}

export async function growRoom(id, members, root = mailboxRoot()) {
  return updateRoom(id, (room) => addMembers(room, members), { root });
}

function memberOf(room, id) {
  const member = id === "conductor" ? room.conductor : room.agents.find((agent) => agent.id === id);
  if (!member) throw new Error(`Unknown member: ${id}`);
  return member;
}

/** An explicit binding is a session reference, never permission to prompt that session. */
export async function bindSession(id, memberId, { session, engine, root = mailboxRoot() }) {
  if (!session || !["claude", "codex", "agy", "opencode"].includes(engine))
    throw new Error("Session binding requires a session ID and supported engine");
  return updateRoom(
    id,
    (room) => {
      const member = memberOf(room, memberId);
      if (member.engine && member.engine !== engine)
        throw new Error("Session engine differs from room member");
      member.session = { id: session, engine, machine: hostname(), cwd: room.cwd };
      return room;
    },
    { root },
  );
}

/** Persist intent before coordinator dispatch; retries use the same task and request. */
export async function dispatchMember(id, memberId, options = {}) {
  const { root = mailboxRoot(), run = coordinator } = options;
  const directory = await realpath(options.taskDir);
  const task = await run(["show", "--task-dir", directory]);
  if (task.schema !== "agent-executor.task.v1" || typeof task.task_id !== "string")
    throw new Error("Unsupported coordinator task");
  const room = await readRoom(id, root);
  if ((await realpath(task.repository)) !== (await realpath(room.cwd)))
    throw new Error("Task repository differs from room checkout");
  // A room under a team only runs that team's tasks, so a superseded lead cannot dodge the fence here.
  if (room.authority && task.authority?.team !== room.authority)
    throw new Error(
      `Task is not under the room's team ${room.authority}; init it with --authority`,
    );
  const kind = options.kind || "investigation";
  if (!["implementation", "investigation", "consultation", "challenge"].includes(kind))
    throw new Error("Unsupported dispatch kind");
  const transport = options.transport || "executor";
  if (!["executor", "native"].includes(transport))
    throw new Error("Unsupported dispatch transport");
  const intent = {
    taskDir: directory,
    taskId: task.task_id,
    requestId: options.requestId || randomUUID(),
    kind,
    transport,
    billingMode: options.billingMode || "unknown",
    ...(options.packet && { packet: await realpath(options.packet) }),
  };
  const saved = await updateRoom(
    id,
    (current) => {
      if (memberId === "conductor") throw new Error("Dispatch a worker, not the conductor");
      const member = memberOf(current, memberId);
      if (member.dispatch) {
        for (const key of ["taskDir", "taskId", "kind", "transport", "billingMode", "packet"])
          if (member.dispatch[key] !== intent[key])
            throw new Error(`Existing dispatch differs: ${key}`);
        if (options.requestId && options.requestId !== member.dispatch.requestId)
          throw new Error("A member already has a dispatch; add a member for new work");
      } else {
        if (
          task.runs.some((entry) => entry.request_id === intent.requestId) ||
          current.agents.some(
            (entry) =>
              entry.id !== memberId &&
              entry.dispatch?.taskId === intent.taskId &&
              entry.dispatch?.requestId === intent.requestId,
          )
        )
          throw new Error("Request ID already belongs to another dispatch");
        member.dispatch = intent;
      }
      member.status = "unknown";
      return current;
    },
    { root },
  );
  const member = memberOf(saved, memberId);
  const binding = member.dispatch;
  const args = [
    "dispatch",
    "--task-dir",
    binding.taskDir,
    "--request-id",
    binding.requestId,
    "--kind",
    binding.kind,
    "--transport",
    binding.transport,
    "--engine",
    member.engine,
    "--model",
    member.model,
    "--billing-mode",
    binding.billingMode,
    "--background",
    ...leadArgs(options),
  ];
  if (member.effort) args.push("--effort", member.effort);
  if (binding.packet) args.push("--packet", binding.packet);
  let result;
  try {
    result = await run(args);
  } catch (error) {
    // The coordinator records a run before any side effect, so a refusal with no run for this
    // request launched nothing: clear the intent so the member can be dispatched again.
    let wasLaunched = true;
    try {
      const after = await run(["show", "--task-dir", binding.taskDir]);
      wasLaunched = after.runs.some((entry) => entry.request_id === binding.requestId);
    } catch {
      // Unknown outcome: keep the intent so retrying the same request can recover it.
    }
    await updateRoom(
      id,
      (current) => {
        const agent = memberOf(current, memberId);
        agent.dispatchError = error.message;
        if (!wasLaunched && agent.dispatch?.requestId === binding.requestId) {
          delete agent.dispatch;
          agent.status = "queued";
        }
        return current;
      },
      { root },
    );
    throw error;
  }
  await updateRoom(
    id,
    (current) => {
      const agent = memberOf(current, memberId);
      // A concurrent refused dispatch of this member may have cleared the intent; this one launched.
      agent.dispatch ||= binding;
      agent.dispatch.runId = result.run_id;
      delete agent.dispatchError;
      return current;
    },
    { root },
  );
  return {
    room: await observeRoom(id, { root, run, authority: options.authority }),
    dispatch: result,
  };
}

/** Read task snapshots through the coordinator. A terminal's idle state is never a verdict. */
export async function observeRoom(
  id,
  { root = mailboxRoot(), run = coordinator, authority = authorityShow } = {},
) {
  const room = await readRoom(id, root);
  if (room.authority) room.lead = await authority(room.authority);
  const tasks = new Map();
  for (const member of room.agents) {
    if (!member.dispatch) continue;
    const binding = member.dispatch;
    try {
      if (!tasks.has(binding.taskDir))
        tasks.set(binding.taskDir, await run(["show", "--task-dir", binding.taskDir]));
      const task = tasks.get(binding.taskDir);
      if (
        task.task_id !== binding.taskId ||
        (await realpath(task.repository)) !== (await realpath(room.cwd))
      )
        throw new Error("Task identity changed");
      const found = task.runs.filter((entry) => entry.request_id === binding.requestId);
      if (found.length !== 1)
        throw new Error("Dispatch has no unique recorded run; retry the same dispatch to recover");
      const record = found[0];
      if (binding.runId && record.run_id !== binding.runId) throw new Error("Run identity changed");
      if (record.engine !== member.engine || record.transport !== binding.transport)
        throw new Error("Run executor differs from room member");
      member.runId = record.run_id;
      member.transport = record.transport;
      member.resultPath = record.result_path;
      member.mailboxSession = record.mailbox?.worker;
      member.verdict =
        task.decisions.findLast((decision) => decision.run_id === record.run_id)?.decision ||
        "pending";
      const states = {
        reserved: "reserved",
        active: "running",
        finished: "review",
        cancelled: "cancelled",
      };
      member.status = Object.hasOwn(states, record.status) ? states[record.status] : "unknown";
      if (record.runner_status && record.runner_status !== "completed") member.status = "failed";
      const verdicts = {
        accept: "completed",
        supported: "completed",
        reject: "rejected",
        takeover: "blocked",
      };
      if (Object.hasOwn(verdicts, member.verdict)) member.status = verdicts[member.verdict];
      member.observedAt = task.updated_at;
      member.nativeHandle = record.native_handle;
      member.objective = task.task_spec?.objective;
      member.output = [
        member.objective,
        `Coordinator observed: ${task.updated_at || "unknown"}`,
        record.recovery,
        member.dispatchError,
        member.resultPath,
      ].filter(Boolean);
    } catch (error) {
      member.status = "unknown";
      member.output = [error.message];
    }
  }
  return room;
}

export async function recoverRoom(id, options = {}) {
  const { root = mailboxRoot(), run = coordinator } = options;
  const room = await readRoom(id, root);
  const directories = new Set(
    room.agents.map((member) => member.dispatch?.taskDir).filter(Boolean),
  );
  for (const directory of directories) {
    const task = await run(["show", "--task-dir", directory]);
    if (
      (await realpath(task.repository)) !== (await realpath(room.cwd)) ||
      room.agents.some(
        (member) =>
          member.dispatch?.taskDir === directory && member.dispatch.taskId !== task.task_id,
      )
    )
      throw new Error("Task identity changed; refusing to recover a different task");
  }
  for (const directory of directories)
    await run(["recover", "--task-dir", directory, ...leadArgs(options)]);
  return observeRoom(id, { root, run, authority: options.authority });
}

export async function readLaunchPlan(file) {
  const plan = JSON.parse(await readFile(file, "utf8"));
  if (!Array.isArray(plan.members) || plan.members.length === 0)
    throw new Error("Launch plan requires members with id and taskDir");
  return plan;
}
