import { shouldUseColor } from "../../monitor/lib/render.mjs";
import { cachedQuota } from "../bin/stooges-tui.mjs";
import {
  addMembers,
  createRoom,
  mailboxRoot,
  parseTeam,
  readRoom,
  updateRoom,
} from "./stooges-room.mjs";
import { StoogesView } from "./stooges-view.mjs";

const KEY = "stooges-room";
const HELP =
  "/stooges <room-id> | new [team] | off | focus <1–9> | pin | next · [?] help · [g] logs";

// Pi accepts JSON Schema; no TypeBox runtime is needed for this object of required strings.
const PARAMETERS = {
  type: "object",
  properties: {
    roomId: {
      type: "string",
      minLength: 1,
      maxLength: 80,
      pattern: "^[A-Za-z0-9_-]+$",
      description: "Existing swarm room ID",
    },
    team: {
      type: "string",
      minLength: 1,
      description: "Team specification, for example claude:haiku*2,codex:luna:max",
    },
  },
  required: ["roomId", "team"],
  additionalProperties: false,
};

/** Load with pi --extension ./plugins/orchestrate/lib/pi-stooges-widget.mjs. */
export default function stoogesExtension(pi) {
  let active;

  const detach = () => {
    if (!active) return;
    const previous = active;
    active = undefined;
    previous.dispose?.();
    previous.ctx.ui.setWidget(KEY, undefined);
  };

  const attach = (room, ctx, root) => {
    detach();
    const view = new StoogesView(room, {
      height: process.stdout.rows || 24,
      color: shouldUseColor(process.stdout),
    });
    const state = { id: room.id, ctx, view };
    active = state;
    ctx.ui.setWidget(KEY, (tui) => {
      let isDisposed = false;
      let isRefreshing = false;
      const refresh = async () => {
        if (isDisposed || isRefreshing) return;
        isRefreshing = true;
        try {
          const next = await readRoom(room.id, root);
          try {
            const accounts = await cachedQuota();
            if (accounts.length > 0) next.accounts = accounts;
          } catch (error) {
            next.quotaError = error.message;
          }
          if (!isDisposed) {
            const isChanged = JSON.stringify(next) !== JSON.stringify(view.room);
            view.setRoom(next);
            if (isChanged) tui.requestRender();
          }
        } catch (error) {
          if (!isDisposed) {
            ctx.ui.notify(`Swarm room refresh stopped: ${error.message}`, "error");
            dispose();
          }
        } finally {
          isRefreshing = false;
        }
      };
      const animation = setInterval(() => {
        if (view.needsAnimation) tui.requestRender();
      }, 200);
      const poll = setInterval(refresh, 1000);
      animation.unref?.();
      poll.unref?.();
      const dispose = () => {
        if (isDisposed) return;
        isDisposed = true;
        clearInterval(animation);
        clearInterval(poll);
      };
      Object.assign(state, { dispose, refresh, requestRender: () => tui.requestRender() });
      return {
        render: (width) => {
          view.height = process.stdout.rows || 24;
          return view.render(width);
        },
        invalidate: () => view.invalidate(),
        handleInput: (data) => {
          view.handleInput(data);
          tui.requestRender();
        },
        dispose,
      };
    });
  };

  pi.registerCommand("stooges", {
    description: "Display and focus a swarm room; new [team], off, focus N, pin, next",
    handler: async (args, ctx) => {
      if (!ctx.hasUI || (ctx.mode && ctx.mode !== "tui"))
        throw new Error("/stooges needs an interactive Pi terminal");
      const input = args.trim();
      if (!input) {
        ctx.ui.notify(HELP, "info");
        return;
      }
      if (input === "off") {
        detach();
        return;
      }
      const focus = /^focus ([1-9])$/u.exec(input);
      if (focus || input === "pin" || input === "next") {
        if (!active) throw new Error("Attach a room first with /stooges <room-id>");
        let key = "n";
        if (focus) key = focus[1];
        else if (input === "pin") key = "p";
        active.view.handleInput(key);
        active.requestRender?.();
        return;
      }
      const root = mailboxRoot();
      let room;
      if (input === "new" || input.startsWith("new ")) {
        const team = input.slice(3).trim() || "claude:haiku,agy:flash,codex:luna:max";
        room = createRoom(parseTeam(team), { cwd: ctx.cwd });
        await updateRoom(room.id, () => room, { root, create: true });
      } else room = await readRoom(input, root);
      attach(room, ctx, root);
    },
  });

  pi.registerTool({
    name: "stooges_spawn",
    label: "Queue swarm members",
    description:
      "Add queued agents to an existing swarm room. The conductor owns execution; this tool never launches models.",
    parameters: PARAMETERS,
    async execute(_callId, params, signal) {
      signal?.throwIfAborted();
      if (
        !params ||
        typeof params.roomId !== "string" ||
        typeof params.team !== "string" ||
        Object.keys(params).some((key) => key !== "roomId" && key !== "team")
      )
        throw new Error("stooges_spawn requires only roomId and team strings");
      const members = parseTeam(params.team);
      let agentIds;
      await updateRoom(params.roomId, (room) => {
        signal?.throwIfAborted();
        const previous = room.agents.length;
        addMembers(room, members);
        agentIds = room.agents.slice(previous).map((agent) => agent.id);
        return room;
      });
      if (active?.id === params.roomId) await active.refresh?.();
      return {
        content: [
          {
            type: "text",
            text: `Queued ${agentIds.length} agents in room ${params.roomId}: ${agentIds.join(", ")}`,
          },
        ],
        details: { roomId: params.roomId, agentIds },
      };
    },
  });

  pi.on("session_shutdown", detach);
}
