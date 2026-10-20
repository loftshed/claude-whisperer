#!/usr/bin/env node
import { realpath } from "node:fs/promises";
import { createInterface } from "node:readline/promises";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

import { herdrClient } from "../lib/herdr-client.mjs";
import { PLUGIN_ID, swarmAdapter } from "../lib/herdr-swarms.mjs";
import {
  coordinator,
  dispatchMember,
  growRoom,
  newRoom,
  observeRoom,
  recoverRoom,
} from "../lib/rooms.mjs";
import { parseTeam } from "../lib/stooges-room.mjs";
import { runOverview } from "../lib/swarm-overview.mjs";
import { cachedQuota, runTui } from "./stooges-tui.mjs";

const HELP = `ork herdr overview [--json]
ork herdr open --room ID
ork herdr focus --room ID --member ID|conductor
ork herdr filter --room ID
ork herdr clear-filter
ork herdr reconcile
ork herdr forget --room ID

Run inside Herdr with the Orkest plugin linked. No default/focused server fallback.
forget clears uncertain layout intent only; inspect Herdr first. It never stops workers.
`;

async function form() {
  const prompts = createInterface({ input: process.stdin, output: process.stdout });
  const ask = async (label, fallback = "") =>
    (await prompts.question(`${label}${fallback ? ` [${fallback}]` : ""}: `)).trim() || fallback;
  try {
    const operation = process.env.ORK_FORM;
    let result;
    switch (operation) {
      case "create": {
        const cwd = await ask("Repository checkout", process.env.ORK_CWD || process.cwd());
        const group = await ask("Group");
        const title = await ask("Swarm title");
        const team = await ask("Team", "claude:haiku,agy:flash,codex:luna:max");
        result = await newRoom(parseTeam(team), { cwd, group, title });
        process.stdout.write(
          `Created ${result.id}. Members are queued. Bind tasks with Dispatch.\n`,
        );

        break;
      }
      case "add": {
        result = await growRoom(process.env.ORK_ROOM_ID, parseTeam(await ask("Team to add")));

        break;
      }
      case "dispatch": {
        const room = await observeRoom(process.env.ORK_ROOM_ID);
        process.stdout.write(
          `${room.agents.map((member) => `${member.id}: ${member.status}`).join("\n")}\n`,
        );
        const member = await ask("Member ID");
        const taskDir = await ask("Coordinator task directory");
        const spec = await ask(
          "Task spec file to initialize a new task, or Enter for existing task",
        );
        if (spec) {
          const host = await ask("Conductor host: codex, claude, agy, opencode, other");
          await coordinator(["init", "--task-spec", spec, "--task-dir", taskDir, "--host", host]);
        }
        const kind = await ask("Kind", "investigation");
        const transport = await ask("Transport", "executor");
        const packet = ["consultation", "challenge"].includes(kind)
          ? await ask("Consultation packet file")
          : undefined;
        const confirmation = await ask(
          "Dispatch may start billable work. Type dispatch to continue",
        );
        if (confirmation !== "dispatch") return;
        result = await dispatchMember(room.id, member, { taskDir, kind, transport, packet });

        break;
      }
      default: {
        throw new Error("Unknown form");
      }
    }
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    await ask("Press Enter to close");
  } finally {
    prompts.close();
  }
}

export async function herdrCommand(argv, { stdout = process.stdout, rpc, adapter } = {}) {
  const { positionals, values } = parseArgs({
    args: argv,
    allowPositionals: true,
    options: {
      room: { type: "string" },
      member: { type: "string" },
      json: { type: "boolean" },
      help: { type: "boolean" },
    },
  });
  const [action] = positionals;
  if (!action || values.help) {
    stdout.write(HELP);
    return;
  }
  rpc ||= herdrClient();
  const service = adapter || swarmAdapter({ rpc });
  const id = values.room || process.env.ORK_ROOM_ID;
  const requireRoom = () => {
    if (!id) throw new Error("--room is required");
    return id;
  };
  if (action === "room") {
    const roomId = requireRoom();
    if (process.env.HERDR_PANE_ID)
      await rpc("pane.report_metadata", {
        pane_id: process.env.HERDR_PANE_ID,
        source: `plugin:${PLUGIN_ID}`,
        tokens: { ork_room_id: roomId, ork_view: "detail" },
      });
    return runTui(() => observeRoom(roomId));
  }
  if (action === "form") return form();
  if (action === "overview" && !values.json) {
    const openForm = (operation, roomId) =>
      rpc("plugin.pane.open", {
        plugin_id: PLUGIN_ID,
        entrypoint: "form",
        placement: "popup",
        focus: true,
        width: "90%",
        height: "80%",
        env: { ORK_FORM: operation, ...(roomId && { ORK_ROOM_ID: roomId }) },
        ...(process.env.HERDR_PANE_ID && { target_pane_id: process.env.HERDR_PANE_ID }),
      });
    return runOverview({
      load: () => service.reconcile(),
      quota: cachedQuota,
      open: service.open,
      focus: service.focus,
      filter: service.filter,
      recover: recoverRoom,
      form: openForm,
    });
  }
  let result;
  switch (action) {
    case "overview":
    case "reconcile": {
      const { rooms, errors } = await service.reconcile();
      result = { rooms, errors };

      break;
    }
    case "open": {
      result = await service.open(requireRoom());
      break;
    }
    case "focus": {
      result = await service.focus(requireRoom(), values.member || "conductor");
      break;
    }
    case "filter": {
      result = await service.filter(requireRoom());
      break;
    }
    case "clear-filter": {
      result = await service.clearFilter();
      break;
    }
    case "forget": {
      result = await service.forget(requireRoom());
      break;
    }
    default: {
      throw new Error(HELP);
    }
  }
  stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  return result;
}

let entry = "";
try {
  if (process.argv[1]) entry = await realpath(process.argv[1]);
} catch {
  /* Imports do not need an executable path. */
}
if (entry === fileURLToPath(import.meta.url)) {
  try {
    await herdrCommand(process.argv.slice(2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
