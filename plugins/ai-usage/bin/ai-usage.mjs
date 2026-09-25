#!/usr/bin/env node
import { parseArgs } from "node:util";
import { existsSync } from "node:fs";
import { expandHome } from "../lib/exec.mjs";
import { detectConfig } from "../lib/init.mjs";
import { serveMcp, summarizeLanes } from "../lib/mcp.mjs";
import { registerMcp } from "../lib/register.mjs";
import { jsonView, renderLine, renderReport, useColor } from "../lib/render.mjs";
import { CACHE_DIR, CONFIG_PATH, getAccounts, loadConfig, writeConfig } from "../lib/store.mjs";
import { VERSION } from "../lib/version.mjs";

const HELP = `ai-usage: remaining quota across your Claude, Codex and Antigravity accounts

Usage:
  ai-usage [show]            table of every window plus where to spend next
  ai-usage watch             live full-screen view (r = refresh now, q = quit)
  ai-usage json              machine-readable snapshot (used by the menu bar app)
  ai-usage line              one-line summary for tmux or a status line
  ai-usage recommend         ranked pools to use next  [--family claude|gpt|gemini]
  ai-usage mcp               run as an MCP server on stdio
  ai-usage init              detect this machine's accounts and write the config [--force] [--dry-run]
  ai-usage config            show config and cache locations
  ai-usage mcp-install       register the MCP server in Claude Code profiles, Codex, agy,
                             Gemini CLI and OpenCode
                             [--dry-run] [--command <launcher path>]
                             [--skip-claude] when Claude Code gets it from the ai-usage plugin
  ai-usage mcp-uninstall     remove those registrations
  ai-usage --version

Options:
  -r, --refresh              ignore the cache and query every provider now
  --max-age <seconds>        reuse cached data younger than this (default from config, 180)
  --interval <seconds>       watch: how often to re-query providers (default 120)
  --family <name>            recommend: claude, gpt or gemini
  --no-color                 plain output
`;

const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    refresh: { type: "boolean", short: "r" },
    "max-age": { type: "string" },
    interval: { type: "string" },
    family: { type: "string" },
    json: { type: "boolean" },
    command: { type: "string" },
    "dry-run": { type: "boolean" },
    force: { type: "boolean" },
    "skip-claude": { type: "boolean" },
    version: { type: "boolean", short: "v" },
    "no-color": { type: "boolean" },
    help: { type: "boolean", short: "h" },
  },
});

const command = positionals[0] ?? "show";
const maxAgeSeconds = values["max-age"] ? Number(values["max-age"]) : undefined;
const color = !values["no-color"] && useColor();

async function show() {
  const accounts = await getAccounts({ force: values.refresh, maxAgeSeconds });
  console.log(renderReport(accounts, { color }));
}

async function recommend() {
  const view = jsonView(await getAccounts({ force: values.refresh, maxAgeSeconds }));
  const lanes = values.family ? view.lanes.filter((l) => l.families.includes(values.family)) : view.lanes;
  if (values.json) return console.log(JSON.stringify(lanes, null, 2));
  console.log(summarizeLanes(lanes));
}

async function watch() {
  const interval = Math.max(30, Number(values.interval ?? 120));
  const out = process.stdout;
  let accounts = null;
  let refreshing = false;
  let timer;
  const draw = () => {
    if (!accounts) return;
    const footer = `r refresh · q quit · re-queries every ${interval}s · ${new Date().toLocaleTimeString()}`;
    out.write(`\x1b[H\x1b[2J${renderReport(accounts, { color, refreshing, footer })}\n`);
  };
  const refresh = async (force = false) => {
    if (refreshing) return;
    refreshing = true;
    draw();
    try {
      accounts = await getAccounts({ force, maxAgeSeconds: interval });
    } finally {
      refreshing = false;
    }
    draw();
  };
  const quit = (reason) => {
    clearInterval(timer);
    out.write("\x1b[?25h\x1b[?1049l");
    if (process.env.AI_USAGE_DEBUG) console.error(`ai-usage watch: quit (${reason})`);
    process.exit(0);
  };
  out.write("\x1b[?1049h\x1b[?25l");
  process.on("SIGINT", () => quit("SIGINT"));
  process.on("SIGTERM", () => quit("SIGTERM"));
  if (process.stdin.isTTY) {
    process.stdin.setRawMode(true);
    process.stdin.resume();
    // Keys can arrive batched in one chunk (pasted, or piped through a pty), so look at each one.
    process.stdin.on("data", (chunk) => {
      for (const k of chunk.toString()) {
        if (k === "q" || k === "\u0003") return quit(`key ${JSON.stringify(k)}`);
        if (k === "r") refresh(true);
      }
    });
  }
  out.on("resize", draw);
  accounts = await getAccounts({ maxAgeSeconds: interval });
  draw();
  // Redraw every 30s so countdowns stay current; re-query only once data is older than the interval.
  timer = setInterval(() => refresh(false), 30_000);
}

function config() {
  console.log(`config: ${CONFIG_PATH}\ncache:  ${CACHE_DIR}\n`);
  console.log(JSON.stringify(loadConfig(), null, 2));
}

function init() {
  const detected = detectConfig();
  if (values["dry-run"] || (existsSync(CONFIG_PATH) && !values.force)) {
    if (!values["dry-run"]) console.error(`${CONFIG_PATH} already exists; pass --force to replace it. Detected on this machine:\n`);
    return console.log(JSON.stringify(detected, null, 2));
  }
  writeConfig(detected);
  console.log(`wrote ${CONFIG_PATH} with ${detected.accounts.length} account(s): ${detected.accounts.map((a) => a.id).join(", ")}`);
  console.log("Edit labels, short names and routes there; see config.example.json for a two-Claude-profile setup.");
}

try {
  if (values.version) console.log(VERSION);
  else if (values.help || command === "help") console.log(HELP);
  else if (command === "show") await show();
  else if (command === "watch") await watch();
  else if (command === "json") console.log(JSON.stringify(jsonView(await getAccounts({ force: values.refresh, maxAgeSeconds })), null, 2));
  else if (command === "line") console.log(renderLine(await getAccounts({ force: values.refresh, maxAgeSeconds })));
  else if (command === "recommend") await recommend();
  else if (command === "mcp") serveMcp();
  else if (command === "config") config();
  else if (command === "mcp-install" || command === "mcp-uninstall") {
    const launcher = expandHome(values.command ?? "~/.local/bin/ai-usage");
    await registerMcp(loadConfig(), { launcher, dryRun: values["dry-run"], remove: command === "mcp-uninstall", skipClaude: values["skip-claude"] });
  } else if (command === "init") init();
  else {
    console.error(`Unknown command "${command}"\n\n${HELP}`);
    process.exitCode = 2;
  }
} catch (err) {
  console.error(`ai-usage: ${err.message}`);
  process.exitCode = 1;
}
