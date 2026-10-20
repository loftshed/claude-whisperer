import { createHash } from "node:crypto";
import { mkdir, readFile, rename, rmdir, writeFile } from "node:fs/promises";
import { hostname } from "node:os";
import path from "node:path";

import { listRooms, observeRoom } from "./rooms.mjs";
import { mailboxRoot } from "./stooges-room.mjs";

export const PLUGIN_ID = "orkest.swarms";
const SOURCE = `plugin:${PLUGIN_ID}`;
const engineKind = { agy: "agy", claude: "claude", codex: "codex", opencode: "opencode" };
const key = (text) => createHash("sha256").update(text).digest("hex").slice(0, 24);
const groupKey = (room) => {
  if (!room.cwd) throw new Error("Herdr rooms require an explicit checkout");
  return key(`${room.cwd}\0${room.group || path.basename(room.cwd)}`);
};

export function matchSession(room, member, panes, machine = hostname()) {
  const reference = member.session;
  if (!reference || reference.machine !== machine || reference.cwd !== room.cwd)
    return { status: "unbound" };
  const matches = panes.filter(
    (pane) =>
      pane.agent_session?.kind === "id" &&
      pane.agent_session.value === reference.id &&
      pane.agent_session.agent === engineKind[reference.engine] &&
      (pane.foreground_cwd || pane.cwd) === room.cwd,
  );
  if (matches.length !== 1) return { status: matches.length > 0 ? "ambiguous" : "unbound" };
  return { status: "bound", pane: matches[0] };
}

/** State is local to this machine/socket, never part of the canonical room file. */
export function swarmAdapter({
  rpc,
  root = mailboxRoot(),
  socket = process.env.HERDR_SOCKET_PATH,
  stateRoot = path.join(root, "herdr"),
  observe = (id) => observeRoom(id, { root }),
} = {}) {
  if (!rpc || !socket) throw new Error("An explicit Herdr session is required");
  const stateFile = path.join(stateRoot, `${key(`${hostname()}\0${socket}`)}.json`);
  const exclusive = async (operation) => {
    await mkdir(stateRoot, { recursive: true, mode: 0o700 });
    const lock = `${stateFile}.lock`;
    try {
      await mkdir(lock);
    } catch (error) {
      if (error.code === "EEXIST")
        throw new Error(
          `Another layout operation owns ${lock}. If it crashed, inspect pending state before removing this empty lock.`,
          { cause: error },
        );
      throw error;
    }
    try {
      return await operation();
    } finally {
      await rmdir(lock);
    }
  };
  const readState = async () => {
    try {
      const state = JSON.parse(await readFile(stateFile, "utf8"));
      if (!state.rooms || !state.groups) throw new Error("Invalid Herdr adapter state");
      Object.setPrototypeOf(state.rooms, null);
      Object.setPrototypeOf(state.groups, null);
      return state;
    } catch (error) {
      if (error.code === "ENOENT")
        return { rooms: Object.create(null), groups: Object.create(null) };
      throw error;
    }
  };
  const save = async (state) => {
    await mkdir(stateRoot, { recursive: true, mode: 0o700 });
    const temporary = `${stateFile}.${process.pid}.tmp`;
    await writeFile(temporary, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
    await rename(temporary, stateFile);
  };
  const snapshot = async () => {
    const result = await rpc("session.snapshot");
    const value = result.snapshot || result;
    if (
      !Array.isArray(value.panes) ||
      !Array.isArray(value.workspaces) ||
      !Array.isArray(value.tabs)
    )
      throw new Error("Unsupported Herdr snapshot");
    return value;
  };
  const reconcile = async () => {
    const live = await snapshot();
    const listing = await listRooms(root);
    const rooms = [];
    for (const entry of listing.rooms) {
      if (entry.machine && entry.machine !== hostname()) continue;
      if (!entry.cwd) {
        listing.errors.push({ file: `${entry.id}.json`, error: "Room has no explicit checkout" });
        continue;
      }
      const room = await observe(entry.id);
      const members = [{ ...room.conductor, id: "conductor" }, ...room.agents];
      const bindings = {};
      for (const member of members) {
        const match = matchSession(room, member, live.panes);
        bindings[member.id] = match;
        if (match.status !== "bound") continue;
        await rpc("pane.report_metadata", {
          pane_id: match.pane.pane_id,
          source: SOURCE,
          ttl_ms: 15_000,
          tokens: {
            ork_group: groupKey(room),
            ork_room_id: room.id,
            ork_member_id: member.id,
            ork_role: member.id === "conductor" ? "conductor" : "worker",
            ork_verdict: member.verdict || member.status || "unknown",
          },
        });
      }
      rooms.push({ ...room, bindings });
    }
    const state = await readState();
    for (const [group, binding] of Object.entries(state.groups)) {
      const workspace = live.workspaces.find(
        (entry) => entry.workspace_id === binding.workspaceId && entry.tokens?.ork_group === group,
      );
      if (!workspace) continue;
      const selected = rooms.filter((room) => groupKey(room) === group);
      await rpc("workspace.report_metadata", {
        workspace_id: workspace.workspace_id,
        source: SOURCE,
        ttl_ms: 15_000,
        tokens: { ork_summary: `${selected.length} swarms` },
      });
    }
    return { rooms, errors: listing.errors, live };
  };
  const open = async (id) => {
    // Serialized by the caller. Never replay a create after losing its response.
    const room = await observe(id);
    const state = await readState();
    const live = await snapshot();
    const previous = state.rooms[id];
    const pane =
      previous &&
      live.panes.find(
        (entry) =>
          entry.pane_id === previous.paneId &&
          entry.terminal_id === previous.terminalId &&
          entry.tokens?.ork_room_id === id &&
          entry.tokens?.ork_view === "detail",
      );
    if (pane) {
      await rpc("pane.focus", { pane_id: pane.pane_id });
      return { roomId: id, paneId: pane.pane_id, reused: true };
    }
    // Metadata can recover a saved view after a lost response or a pane move.
    const candidates = live.panes.filter(
      (entry) => entry.tokens?.ork_room_id === id && entry.tokens?.ork_view === "detail",
    );
    if (candidates.length === 1) {
      const restored = candidates[0];
      state.rooms[id] = { paneId: restored.pane_id, terminalId: restored.terminal_id };
      await save(state);
      await rpc("pane.focus", { pane_id: restored.pane_id });
      return { roomId: id, paneId: restored.pane_id, reused: true };
    }
    if (candidates.length > 1) throw new Error("Multiple swarm views exist; focus one explicitly");
    if (previous)
      throw new Error(
        "Previous swarm view is missing or uncertain. Inspect Herdr before clearing it with forget.",
      );
    const group = groupKey(room);
    const oldGroup = state.groups[group];
    const taggedGroups = live.workspaces.filter((entry) => entry.tokens?.ork_group === group);
    if (taggedGroups.length > 1)
      throw new Error("Multiple workspaces claim this group; inspect Herdr before opening");
    let workspace = live.workspaces.find(
      (entry) =>
        entry.workspace_id === oldGroup?.workspaceId &&
        (entry.tokens?.ork_group === group ||
          live.panes.some(
            (pane) =>
              pane.workspace_id === entry.workspace_id &&
              pane.terminal_id === oldGroup.rootTerminalId,
          )),
    );
    workspace ||= taggedGroups[0];
    if (oldGroup && !workspace)
      throw new Error(
        "Previous group layout is missing or uncertain. Inspect Herdr before clearing it with forget.",
      );
    state.rooms[id] = { pending: true };
    if (!workspace) state.groups[group] = { pending: true };
    await save(state);
    let target;
    if (workspace) {
      await rpc("workspace.report_metadata", {
        workspace_id: workspace.workspace_id,
        source: SOURCE,
        tokens: { ork_group: group },
      });
      const created = await rpc("tab.create", {
        workspace_id: workspace.workspace_id,
        cwd: room.cwd,
        label: room.title || id,
        focus: false,
      });
      target = created.root_pane;
      state.groups[group] = {
        workspaceId: workspace.workspace_id,
        rootTerminalId: target.terminal_id,
      };
      await save(state);
    } else {
      const created = await rpc("workspace.create", {
        cwd: room.cwd,
        label: room.group || path.basename(room.cwd),
        focus: false,
      });
      workspace = created.workspace;
      target = created.root_pane;
      state.groups[group] = {
        workspaceId: workspace.workspace_id,
        rootTerminalId: target.terminal_id,
      };
      await save(state);
      await rpc("workspace.report_metadata", {
        workspace_id: workspace.workspace_id,
        source: SOURCE,
        tokens: { ork_group: group },
      });
    }
    const opened = await rpc("plugin.pane.open", {
      plugin_id: PLUGIN_ID,
      entrypoint: "room",
      placement: "split",
      focus: true,
      target_pane_id: target.pane_id,
      env: { ORK_ROOM_ID: id },
    });
    const detail = opened.pane || opened.plugin_pane?.pane;
    if (!detail?.pane_id || !detail.terminal_id)
      throw new Error("Herdr did not return a plugin pane; reconcile before retrying");
    await rpc("pane.report_metadata", {
      pane_id: detail.pane_id,
      source: SOURCE,
      tokens: { ork_room_id: id, ork_view: "detail" },
    });
    state.rooms[id] = { paneId: detail.pane_id, terminalId: detail.terminal_id };
    await save(state);
    return { roomId: id, paneId: detail.pane_id, reused: false };
  };
  return {
    reconcile,
    open: (id) => exclusive(() => open(id)),
    async focus(id, memberId) {
      const room = await observe(id);
      const member =
        memberId === "conductor"
          ? room.conductor
          : room.agents.find((entry) => entry.id === memberId);
      if (!member) throw new Error("Unknown member");
      const match = matchSession(room, member, (await snapshot()).panes);
      if (match.status !== "bound")
        throw new Error(`Member is ${match.status}; open its room for status and results`);
      await rpc("pane.focus", { pane_id: match.pane.pane_id });
      return { paneId: match.pane.pane_id };
    },
    async filter(id) {
      await observe(id);
      return rpc("agent.view.set", {
        source: SOURCE,
        label: "Orkest swarm",
        filter: { op: "eq", field: { token: "ork_room_id" }, value: id },
        sort: [{ field: "attention", order: "desc" }],
      });
    },
    clearFilter: () => rpc("agent.view.clear", { source: SOURCE }),
    forget: (id) =>
      exclusive(async () => {
        const room = await observe(id);
        const state = await readState();
        delete state.rooms[id];
        const group = groupKey(room);
        const binding = state.groups[group];
        const live = await snapshot();
        if (
          binding?.pending ||
          live.panes.every(
            (pane) =>
              !(
                pane.workspace_id === binding?.workspaceId &&
                pane.terminal_id === binding?.rootTerminalId
              ),
          )
        )
          delete state.groups[group];
        await save(state);
        return { forgotten: id, workersStopped: false };
      }),
  };
}
