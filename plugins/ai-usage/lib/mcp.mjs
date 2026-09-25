import { createInterface } from "node:readline";
import { jsonView } from "./render.mjs";
import { getAccounts } from "./store.mjs";
import { VERSION } from "./version.mjs";

const INSTRUCTIONS = `Reports remaining subscription quota on this machine's AI accounts (Claude, Codex/ChatGPT, Antigravity/Gemini) so work can be routed where capacity would otherwise go unused.
Percentages are relative to each account's own allowance and are not comparable in absolute tokens across providers.
surplusPts = weekly % remaining minus what even use until reset would leave; positive means under-used capacity that is lost at reset unless spent.
Switching accounts can switch billing context (e.g. work vs personal); follow the user's rules about which accounts an agent may use.`;

const TOOLS = [
  {
    name: "get_usage",
    description:
      "Current remaining quota for every configured AI account: each rate-limit window (5-hour, weekly, per-model) with % left and reset time, plus a ranked list of where to spend next. Cached for a few minutes; pass refresh=true to re-read now (takes ~5s).",
    inputSchema: {
      type: "object",
      properties: { refresh: { type: "boolean", description: "Bypass the cache and query every provider now." } },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: "recommend",
    description:
      "Ranks the AI accounts/model pools to use next, best first, using remaining quota, time to reset, and whether the account is ahead of or behind even pace. Filter by model family (claude, gpt, gemini). Each entry includes a route hint: the CLI that uses that pool.",
    inputSchema: {
      type: "object",
      properties: {
        family: { type: "string", enum: ["any", "claude", "gpt", "gemini"], description: "Only consider pools serving this model family." },
        refresh: { type: "boolean", description: "Bypass the cache and query every provider now." },
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
];

function summarizeUsage(view) {
  const lines = view.accounts.map((a) => {
    const windows = a.windows
      .map((w) => `${w.label} ${Math.floor(w.remainingPct)}% left${w.resetsInMinutes != null ? ` (resets in ${Math.round(w.resetsInMinutes / 6) / 10}h)` : ""}`)
      .join(", ");
    const problem = a.error ? ` [refresh failed: ${a.error}]` : "";
    return `- ${a.label}: ${windows || "no data"}${problem}`;
  });
  return lines.join("\n");
}

export function summarizeLanes(lanes) {
  let rank = 0;
  return lanes
    .filter((l) => !l.redundant)
    .map((l) => {
      if (l.status !== "available") return `✗ ${l.label}${l.route ? ` [${l.route}]` : ""}: ${l.advice}`;
      const pts = l.surplusPts != null ? `, ${l.surplusPts >= 0 ? "+" : ""}${l.surplusPts} pts vs even pace` : "";
      return `${++rank}. ${l.label}${l.route ? ` [${l.route}]` : ""}: ${Math.floor(l.availableNowPct)}% usable now${pts}; ${l.advice}`;
    })
    .join("\n");
}

async function callTool(name, args = {}) {
  const accounts = await getAccounts({ force: Boolean(args.refresh) });
  const view = jsonView(accounts);
  if (name === "get_usage") {
    return `${summarizeUsage(view)}\n\nWhere to spend next:\n${summarizeLanes(view.lanes)}\n\n${JSON.stringify(view)}`;
  }
  if (name === "recommend") {
    const family = args.family && args.family !== "any" ? args.family : null;
    const lanes = family ? view.lanes.filter((l) => l.families.includes(family)) : view.lanes;
    if (lanes.length === 0) return `No configured account serves the ${family} family.`;
    const best = lanes.find((l) => l.status === "available");
    const head = best ? `Best right now: ${best.label}${best.route ? ` (use: ${best.route})` : ""}. ${best.advice}.` : "Every matching pool is exhausted right now.";
    return `${head}\n\n${summarizeLanes(lanes)}\n\n${JSON.stringify({ schema: view.schema, generatedAt: view.generatedAt, lanes })}`;
  }
  throw Object.assign(new Error(`Unknown tool: ${name}`), { code: -32602 });
}

export function serveMcp() {
  const write = (msg) => process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", ...msg })}\n`);
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on("line", async (line) => {
    if (!line.trim()) return;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      return write({ id: null, error: { code: -32700, message: "Parse error" } });
    }
    const { id, method, params } = msg;
    if (id === undefined || id === null) return; // notification
    try {
      if (method === "initialize") {
        return write({
          id,
          result: {
            protocolVersion: params?.protocolVersion ?? "2025-06-18",
            capabilities: { tools: {} },
            serverInfo: { name: "ai-usage", version: VERSION },
            instructions: INSTRUCTIONS,
          },
        });
      }
      if (method === "ping") return write({ id, result: {} });
      if (method === "tools/list") return write({ id, result: { tools: TOOLS } });
      if (method === "tools/call") {
        if (!TOOLS.some((t) => t.name === params?.name)) return write({ id, error: { code: -32602, message: `Unknown tool: ${params?.name}` } });
        try {
          const text = await callTool(params.name, params.arguments);
          return write({ id, result: { content: [{ type: "text", text }] } });
        } catch (err) {
          return write({ id, result: { content: [{ type: "text", text: `ai-usage failed: ${err.message}` }], isError: true } });
        }
      }
      return write({ id, error: { code: -32601, message: `Method not found: ${method}` } });
    } catch (err) {
      return write({ id, error: { code: -32603, message: err.message } });
    }
  });
}
