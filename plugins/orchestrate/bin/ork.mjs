#!/usr/bin/env node
import { execFile } from "node:child_process";
import { readFile, realpath } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs, promisify } from "node:util";

import { jsonView, renderLine, renderReport, shouldUseColor } from "../../monitor/lib/render.mjs";
import { getAccounts } from "../../monitor/lib/store.mjs";
import {
  addMembers,
  createRoom,
  loadPresets,
  mailboxRoot,
  parseTeam,
  readRoom,
  updateRoom,
  validateRoom,
} from "../lib/stooges-room.mjs";
import {
  glyphSet,
  glyphTier,
  progressBar,
  renderPanel,
  renderTable,
  StoogesView,
  wrapText,
} from "../lib/stooges-view.mjs";
import { roomsCommand } from "./rooms.mjs";
import { cachedQuota, runTui } from "./stooges-tui.mjs";

const HELP = `ork rooms --help
ork name [--model <model>] [--effort <level>] [--harness <harness>]
ork routes [--archetype <name>] [--json] [--width 120]
ork quota [--line|--json] [--refresh]
ork telemetry [--runs-dir <directory>] [--json] [--width 120]
ork stooges [--team "claude:haiku*2,agy:flash*2,codex:luna:max"]
ork stooges --preset scout-swarm|bug-court|patch-race|heavy-trifecta
ork stooges spawn --attach-room <id> --team <team>
ork stooges --attach-room <id> [--print|--json]
ork stooges --fixture <room.json> [--print|--json] [--width 120]

New members are queued for the conductor to orchestrate.
Use --cwd to choose the room's repository. Closing the view leaves workers running.
1–9 focus an agent; p pins its detail; n advances the page; q exits the display.
? opens key help; g opens the coalesced room log. Set ORK_GLYPHS=ascii|unicode|nerd to choose a glyph tier.
`;

const execute = promisify(execFile);
const scripts = fileURLToPath(new URL("../skills/orchestrate/scripts/", import.meta.url));
const ARCHETYPES = [
  "debug-invariants",
  "structural-refactor",
  "ui-design-system",
  "architecture-conductor",
  "mechanical-sweep",
  "independent-review",
];
// Reuse effective route/family loading, including user overrides, without resolving live models.
const ROUTES_QUERY = `import json, sys
sys.path.insert(0, sys.argv[1])
import routes, families
effective = routes.load_routes()
definitions = families.load_families(routes.load_config())
rows = []
for archetype in json.loads(sys.argv[2]):
    candidates = routes.route_candidates(archetype, effective)
    for candidate in candidates:
        family, _, tier = candidate['model'].removeprefix('latest:').partition(':')
        definition = definitions.get(family, {})
        tier = tier or definition.get('default_tier', '')
        candidate['tier_strengths'] = definition.get('tier_strengths', {}).get(tier, [])
    rows.append({'archetype': archetype, 'candidates': candidates})
print(json.dumps({'schema': 'ork.routes.v1', 'routes': rows}))
`;

async function pythonJson(args) {
  try {
    const { stdout } = await execute("python3", args, { maxBuffer: 8 * 1024 * 1024 });
    return JSON.parse(stdout);
  } catch (error) {
    throw new Error(`Could not read local routing data: ${error.stderr?.trim() || error.message}`, {
      cause: error,
    });
  }
}

function wrap(value, width) {
  return wrapText(value, width);
}

function framedLines(title, summary, body, width, { color = false, tier = glyphTier() } = {}) {
  return renderPanel(title, [summary, ...body], width, { color, glyphs: glyphSet(tier) })
    .map((line) => line.trimEnd())
    .join("\n");
}

function routeTable(data, width, options = {}) {
  const tier = options.tier || glyphTier();
  const summary = `${data.routes.length} archetypes · ordered routes from local configuration`;
  const bodyWidth = Math.max(4, width - 6);
  let body;
  if (width >= 80) {
    const tableRows = data.routes.flatMap(({ archetype, candidates }) =>
      candidates.map((candidate, index) => [
        index === 0 ? archetype : "",
        index === 0 ? "primary" : `alt ${index}`,
        `${candidate.engine} / ${candidate.model}${candidate.effort || candidate.variant ? ` / ${candidate.effort || candidate.variant}` : ""}`,
        candidate.tier_strengths.join(", ") || "No tier metadata",
      ]),
    );
    body = renderTable(
      ["Task archetype", "Rank", "Engine / model", "Tier strengths"],
      tableRows,
      bodyWidth,
      {
        weights: [0.24, 0.1, 0.28, 0.38],
        tier,
      },
    );
  } else if (width >= 64) {
    const compactRows = data.routes.flatMap(({ archetype, candidates }) =>
      candidates.map((candidate, index) => [
        `${archetype} · ${index === 0 ? "primary" : `alt ${index}`}`,
        `${candidate.engine} / ${candidate.model} / ${candidate.effort || candidate.variant || "default"}`,
        candidate.tier_strengths.join(", ") || "No tier metadata",
      ]),
    );
    body = renderTable(["Task / rank", "Route", "Strengths"], compactRows, bodyWidth, {
      weights: [0.34, 0.34, 0.32],
      tier,
    });
  } else {
    body = data.routes.flatMap(({ archetype, candidates }) => [
      archetype,
      ...candidates.flatMap((candidate, index) => [
        ...wrap(
          `  ${index === 0 ? "primary" : `alt ${index}`} · ${candidate.engine} / ${candidate.model} / ${candidate.effort || candidate.variant || "default"}`,
          bodyWidth,
        ),
        ...wrap(
          `  Strengths: ${candidate.tier_strengths.join(", ") || "No tier metadata"}`,
          bodyWidth,
        ),
      ]),
      "",
    ]);
  }
  return framedLines("Routes", summary, body, width, options);
}

const percent = (value) =>
  typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "--";

function telemetryTable(data, width, options = {}) {
  const tier = options.tier || glyphTier();
  const heading = `${data.runs} runs · ${data.skipped_results} skipped receipts`;
  const bodyWidth = Math.max(4, width - 6);
  if (data.groups.length === 0)
    return framedLines("Telemetry", heading, ["No local run receipts found."], width, options);
  const footer = "Wilson lower bound gives a conservative 95% acceptance estimate.";
  let body;
  if (width >= 80) {
    const rows = data.groups.map((group) => [
      `${group.task_archetype} · ${group.engine}/${group.model}/${group.effort}`,
      String(group.runs),
      `${progressBar(group.accepted_rate, 5, tier)} ${percent(group.accepted_rate)}`,
      percent(group.first_pass_rate),
      `${progressBar(group.wilson_lb_95, 5, tier)} ${percent(group.wilson_lb_95)}`,
      group.median_duration_ms == null ? "--" : `${(group.median_duration_ms / 1000).toFixed(1)}s`,
    ]);
    body = [
      ...renderTable(
        ["Task / model", "Runs", "Accepted", "First pass", "95% lower", "Median"],
        rows,
        bodyWidth,
        {
          weights: [0.29, 0.07, 0.18, 0.16, 0.18, 0.12],
          tier,
        },
      ),
      ...wrap(footer, bodyWidth),
    ];
  } else if (width >= 64) {
    const rows = data.groups.map((group) => [
      `${group.task_archetype} · ${group.engine}/${group.model}`,
      `${progressBar(group.accepted_rate, 4, tier)} ${percent(group.accepted_rate)}`,
      `${progressBar(group.wilson_lb_95, 4, tier)} ${percent(group.wilson_lb_95)}`,
    ]);
    body = [
      ...renderTable(["Task / model", "Accepted", "95% confidence"], rows, bodyWidth, {
        weights: [0.52, 0.23, 0.25],
        tier,
      }),
      ...wrap(footer, bodyWidth),
    ];
  } else {
    body = data.groups.flatMap((group) => [
      ...wrap(
        `${group.task_archetype} · ${group.engine}/${group.model}/${group.effort}`,
        bodyWidth,
      ),
      ...wrap(
        `Accepted ${progressBar(group.accepted_rate, 4, tier)} ${percent(group.accepted_rate)} · first pass ${percent(group.first_pass_rate)}`,
        bodyWidth,
      ),
      ...wrap(
        `95% confidence ${progressBar(group.wilson_lb_95, 4, tier)} ${percent(group.wilson_lb_95)} · ${group.runs} runs · median ${group.median_duration_ms == null ? "--" : `${(group.median_duration_ms / 1000).toFixed(1)}s`}`,
        bodyWidth,
      ),
      "",
    ]);
    body.push(...wrap(footer, bodyWidth));
  }
  return framedLines("Telemetry", heading, body, width, options);
}

async function summaryCommand(argv, stdout) {
  const command = argv[0];
  const common = { help: { type: "boolean", short: "h" }, json: { type: "boolean" } };
  const options =
    command === "quota"
      ? { ...common, line: { type: "boolean" }, refresh: { type: "boolean" } }
      : {
          ...common,
          width: { type: "string" },
          ...(command === "routes"
            ? { archetype: { type: "string" } }
            : { "runs-dir": { type: "string" } }),
        };
  const { values } = parseArgs({ args: argv.slice(1), options });
  if (values.help) {
    stdout.write(HELP);
    return;
  }
  if (command === "quota") {
    if (values.line && values.json) throw new Error("Choose --line or --json");
    const accounts = await getAccounts({ force: values.refresh === true });
    let output;
    if (values.json) output = JSON.stringify(jsonView(accounts), null, 2);
    else if (values.line) output = renderLine(accounts);
    else output = renderReport(accounts, { color: shouldUseColor(stdout) });
    stdout.write(`${output}\n`);
    return accounts;
  }
  const width = Number(values.width || stdout.columns || 120);
  if (!Number.isSafeInteger(width) || width < 40 || width > 1000)
    throw new Error("Width must be 40–1000");
  let data;
  if (command === "routes") {
    if (values.archetype && !ARCHETYPES.includes(values.archetype))
      throw new Error(`Unknown task archetype: ${values.archetype}`);
    const selected = values.archetype ? [values.archetype] : ARCHETYPES;
    data = await pythonJson(["-c", ROUTES_QUERY, scripts, JSON.stringify(selected)]);
  } else {
    const args = [path.join(scripts, "telemetry.py"), "summarize"];
    if (values["runs-dir"]) args.push("--runs-dir", values["runs-dir"]);
    data = await pythonJson(args);
  }
  let output;
  if (values.json) output = JSON.stringify(data, null, 2);
  else if (command === "routes")
    output = routeTable(data, width, { color: shouldUseColor(stdout), tier: glyphTier() });
  else output = telemetryTable(data, width, { color: shouldUseColor(stdout), tier: glyphTier() });
  stdout.write(`${output}\n`);
  return data;
}

export async function main(argv = process.argv.slice(2), { stdout = process.stdout } = {}) {
  if (argv[0] === "rooms") return roomsCommand(argv.slice(1), { stdout });
  if (argv[0] === "name") {
    // A readable session name for the current checkout: claude --name "$(ork name --model opus-5.5)".
    const script = fileURLToPath(
      new URL("../skills/orchestrate/scripts/peer_mailbox.py", import.meta.url),
    );
    const { stdout: name } = await execute("python3", [script, "name", ...argv.slice(1)]);
    stdout.write(name);
    return;
  }
  if (["routes", "quota", "telemetry"].includes(argv[0])) return summaryCommand(argv, stdout);
  const { values, positionals } = parseArgs({
    args: argv,
    allowPositionals: true,
    options: {
      help: { type: "boolean", short: "h" },
      team: { type: "string" },
      preset: { type: "string" },
      "attach-room": { type: "string" },
      fixture: { type: "string" },
      print: { type: "boolean" },
      json: { type: "boolean" },
      width: { type: "string" },
      cwd: { type: "string" },
    },
  });
  if (values.help || positionals.length === 0) {
    stdout.write(HELP);
    return;
  }
  if (
    positionals[0] !== "stooges" ||
    positionals.length > 2 ||
    (positionals[1] && positionals[1] !== "spawn")
  )
    throw new Error("Expected ork stooges or ork stooges spawn; use --help");
  if (values.team && values.preset) throw new Error("Choose --team or --preset");
  if (values.json && values.print) throw new Error("Choose --json or --print");
  const width = Number(values.width || stdout.columns || 100);
  if (!Number.isSafeInteger(width) || width < 1 || width > 1000)
    throw new Error("Width must be 1–1000");
  const isSpawning = positionals[1] === "spawn";
  if (isSpawning && !values["attach-room"]) throw new Error("spawn requires --attach-room");
  if (
    values.fixture &&
    (isSpawning || values["attach-room"] || values.team || values.preset || values.cwd)
  )
    throw new Error("--fixture is an offline view and cannot change or launch a room");
  const root = mailboxRoot();
  let room;
  let load;
  if (values.fixture) {
    load = async () => validateRoom(JSON.parse(await readFile(values.fixture, "utf8")));
  } else {
    let members;
    if (values.preset) {
      const preset = (await loadPresets())[values.preset];
      if (!preset) throw new Error(`Unknown preset: ${values.preset}`);
      members = parseTeam(preset.team);
    } else members = parseTeam(values.team || "claude:haiku,agy:flash,codex:luna:max");
    if (values["attach-room"]) {
      room = await readRoom(values["attach-room"], root);
      if (!isSpawning && (values.team || values.preset || values.cwd))
        throw new Error("Use spawn to add agents; --attach-room alone observes the room");
      if (isSpawning) {
        if (!values.team && !values.preset) throw new Error("spawn requires --team or --preset");
        if (values.cwd && path.resolve(values.cwd) !== room.cwd)
          throw new Error("An attached room keeps its repository");
        room = await updateRoom(
          room.id,
          (current) => {
            addMembers(current, members);
            return current;
          },
          { root },
        );
      }
    } else {
      room = createRoom(members, { cwd: values.cwd });
      await updateRoom(room.id, () => room, { root, create: true });
    }
    load = () => readRoom(room.id, root);
  }
  room = await load();
  if (!values.fixture) {
    try {
      const accounts = await cachedQuota();
      if (accounts.length > 0) room.accounts = accounts;
    } catch (error) {
      room.quotaError = error.message;
    }
  }
  if (values.json) stdout.write(`${JSON.stringify(room, null, 2)}\n`);
  else if (values.print || !stdout.isTTY)
    stdout.write(
      `${new StoogesView(room, { color: shouldUseColor(stdout), tier: glyphTier() })
        .render(width)
        .map((line) => line.trimEnd())
        .join("\n")}\n`,
    );
  else await runTui(load, { output: stdout, quota: !values.fixture });
  return room;
}

let entry = "";
try {
  if (process.argv[1]) entry = await realpath(process.argv[1]);
} catch {
  /* An importing test runner may not have a filesystem entry point. */
}
if (entry === fileURLToPath(import.meta.url)) {
  try {
    await main();
  } catch (error) {
    console.error(`ork: ${error.message}`);
    process.exitCode = 1;
  }
}
