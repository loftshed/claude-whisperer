import path from "node:path";
import { parseArgs } from "node:util";

import {
  bindSession,
  coordinator,
  dispatchMember,
  growRoom,
  listRooms,
  newRoom,
  observeRoom,
  readLaunchPlan,
  recoverRoom,
} from "../lib/rooms.mjs";
import { loadPresets, mailboxRoot, parseTeam } from "../lib/stooges-room.mjs";

export const ROOMS_HELP = `ork rooms list [--json]
ork rooms show --room ID [--json]
ork rooms create --team TEAM [--group GROUP] [--title TITLE] [--cwd PATH] [--id ID]
ork rooms add --room ID --team TEAM
ork rooms init-task --task-spec FILE --task-dir DIR --host codex|claude|agy|opencode|other
ork rooms dispatch --room ID --member ID --task-dir DIR [--kind investigation|implementation|consultation|challenge]
                   [--transport executor|native] [--packet FILE] [--request-id ID]
ork rooms launch --room ID --plan FILE
ork rooms bind --room ID --member ID|conductor --session ID --engine ENGINE
ork rooms recover --room ID

create/add queue members. dispatch/launch use the coordinator and may start billable work.
Native dispatch returns a reservation for the conductor's native tools.
launch plan: {"members":[{"id":"haiku-1","taskDir":"/absolute/task","kind":"investigation"}]}
Task specs and task directories use the existing coordinator contract.
`;

export async function roomsCommand(
  argv,
  { stdout = process.stdout, root = mailboxRoot(), run = coordinator } = {},
) {
  const { positionals, values } = parseArgs({
    args: argv,
    allowPositionals: true,
    options: {
      help: { type: "boolean" },
      json: { type: "boolean" },
      ...Object.fromEntries(
        [
          "room",
          "id",
          "team",
          "preset",
          "cwd",
          "group",
          "title",
          "member",
          "task-dir",
          "task-spec",
          "host",
          "kind",
          "transport",
          "packet",
          "request-id",
          "session",
          "engine",
          "plan",
          "billing-mode",
        ].map((name) => [name, { type: "string" }]),
      ),
    },
  });
  const [action] = positionals;
  if (!action || values.help) {
    stdout.write(ROOMS_HELP);
    return;
  }
  const requireValue = (name) => {
    if (values[name] === undefined || values[name] === "") throw new Error(`--${name} is required`);
    return values[name];
  };
  const team = async () => {
    if (values.team && values.preset) throw new Error("Choose --team or --preset");
    if (values.preset) {
      const preset = (await loadPresets())[values.preset];
      if (!preset) throw new Error("Unknown preset");
      return parseTeam(preset.team);
    }
    return parseTeam(requireValue("team"));
  };
  let result;
  switch (action) {
    case "list": {
      const listing = await listRooms(root);
      result = {
        ...listing,
        rooms: await Promise.all(listing.rooms.map((room) => observeRoom(room.id, { root, run }))),
      };

      break;
    }
    case "create": {
      result = await newRoom(await team(), {
        root,
        id: values.id,
        cwd: values.cwd,
        group: values.group,
        title: values.title,
      });

      break;
    }
    case "add": {
      result = await growRoom(requireValue("room"), await team(), root);

      break;
    }
    case "show": {
      result = await observeRoom(requireValue("room"), { root, run });

      break;
    }
    case "init-task": {
      result = await run([
        "init",
        "--task-spec",
        path.resolve(requireValue("task-spec")),
        "--task-dir",
        path.resolve(requireValue("task-dir")),
        "--host",
        requireValue("host"),
      ]);

      break;
    }
    case "dispatch": {
      result = await dispatchMember(requireValue("room"), requireValue("member"), {
        root,
        run,
        taskDir: requireValue("task-dir"),
        kind: values.kind,
        transport: values.transport,
        packet: values.packet,
        requestId: values["request-id"],
        billingMode: values["billing-mode"],
      });

      break;
    }
    case "launch": {
      const plan = await readLaunchPlan(requireValue("plan"));
      const roomId = requireValue("room");
      // Stop on first failure. Completed requests are safe to revisit using their persisted IDs.
      result = [];
      for (const member of plan.members)
        result.push(await dispatchMember(roomId, member.id, { ...member, root, run }));

      break;
    }
    case "bind": {
      result = await bindSession(requireValue("room"), requireValue("member"), {
        root,
        session: requireValue("session"),
        engine: requireValue("engine"),
      });

      break;
    }
    case "recover": {
      result = await recoverRoom(requireValue("room"), { root, run });

      break;
    }
    default: {
      throw new Error(ROOMS_HELP);
    }
  }
  stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  return result;
}
