import { createInterface } from "node:readline";
import { jsonView } from "./render.mjs";
import { getAccounts } from "./store.mjs";
import { VERSION } from "./version.mjs";

const INSTRUCTIONS = `Reports remaining subscription quota on this machine's AI accounts (Claude, Codex/ChatGPT, Antigravity/Gemini) so work can be routed where capacity would otherwise go unused.
Percentages are relative to each account's own allowance and are not comparable in absolute tokens across providers.
surplusPts = weekly % remaining minus what even use until reset would leave; positive means under-used capacity that is lost at reset unless spent.
Pools marked expiring are near their weekly rollover with capacity left; that remainder is lost at the rollover, so prefer them (and larger tasks) until it is used. Pools under 10% usable now can only take small tasks.
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
      .map((w) => {
        const left = w.exhausted ? "exhausted" : w.blockedBy ? `unusable (${w.blockedBy} limit exhausted)` : `${Math.floor(w.remainingPct)}% left`;
        return `${w.label} ${left}${w.resetsInMinutes != null ? ` (resets in ${Math.round(w.resetsInMinutes / 6) / 10}h)` : ""}`;
      })
      .join(", ");
    const problem = a.error ? ` [refresh failed: ${a.error}]` : "";
    return `- ${a.label}: ${windows || "no data"}${problem}`;
  });
  return lines.join("\n");
}

/** Lines calling out pools whose leftover capacity is lost at an imminent rollover. */
function expiringSummary(lanes) {
  const expiring = lanes.filter((l) => l.expiring && !l.redundant);
  if (expiring.length === 0) return "";
  const hours = (l) => Math.max(0, Math.round((Date.parse(l.expiresAt) - Date.now()) / 3_600_000));
  return `Expiring soon, spend first: ${expiring.map((l) => `${l.label} (${Math.floor(l.weeklyRemainingPct)}% left, resets in ${hours(l)}h at ${l.expiresAt}; ~${l.burnPctPerHour}%/h uses it all)`).join("; ")}\n\n`;
}

// What agents route on, without the display-only fields (pills, sections, headline) that double the tokens.
function compactView(view) {
  const billing = Object.fromEntries(view.accounts.map((a) => [a.id, a.billing]));
  return {
    schema: view.schema,
    generatedAt: view.generatedAt,
    accounts: view.accounts.map((a) => ({
      id: a.id, label: a.label, provider: a.provider, billing: a.billing, ok: a.ok, error: a.error, ageMinutes: a.ageMinutes,
      windows: a.windows.map((w) => ({
        label: w.label, remainingPct: Math.round(w.remainingPct), resetsAt: w.resetsAt,
        ...(w.exhausted && { exhausted: true }), ...(w.blockedBy && { blockedBy: w.blockedBy }),
      })),
    })),
    lanes: compactLanes(view.lanes, billing),
  };
}

function compactLanes(lanes, billing) {
  return lanes.filter((l) => !l.redundant).map((l) => ({
    label: l.label, accountId: l.accountId, poolId: l.poolId, billing: billing[l.accountId] ?? null, families: l.families,
    status: l.status, availableNowPct: Math.round(l.availableNowPct), surplusPts: l.surplusPts, blockedUntil: l.blockedUntil,
    ...(l.expiring && { expiring: true, expiresAt: l.expiresAt, burnPctPerHour: l.burnPctPerHour }),
    route: l.route, advice: l.advice,
  }));
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

async function callTool(name, rawArgs) {
  const args = rawArgs && typeof rawArgs === "object" ? rawArgs : {};
  const accounts = await getAccounts({ force: args.refresh === true });
  const view = jsonView(accounts);
  if (name === "get_usage") {
    return `${expiringSummary(view.lanes)}${summarizeUsage(view)}\n\nWhere to spend next:\n${summarizeLanes(view.lanes)}\n\n${JSON.stringify(compactView(view))}`;
  }
  if (name === "recommend") {
    const family = args.family && args.family !== "any" ? args.family : null;
    const lanes = family ? view.lanes.filter((l) => l.families.includes(family)) : view.lanes;
    if (lanes.length === 0) return `No configured account serves the ${family} family.`;
    const best = lanes.find((l) => l.status === "available");
    const head = best ? `Best right now: ${best.label}${best.route ? ` (use: ${best.route})` : ""}. ${best.advice}.` : "Every matching pool is exhausted right now.";
    const billing = Object.fromEntries(view.accounts.map((a) => [a.id, a.billing]));
    return `${expiringSummary(lanes)}${head}\n\n${summarizeLanes(lanes)}\n\n${JSON.stringify({ schema: view.schema, generatedAt: view.generatedAt, lanes: compactLanes(lanes, billing) })}`;
  }
  throw Object.assign(new Error(`Unknown tool: ${name}`), { code: -32602 });
}

const response = (id, body) => ({ jsonrpc: "2.0", id, ...body });

/** One JSON-RPC message in, one response out (null for notifications). */
async function handle(msg) {
  if (!msg || typeof msg !== "object" || Array.isArray(msg)) return response(null, { error: { code: -32600, message: "Invalid Request" } });
  const { id, method, params } = msg;
  if (id === undefined || id === null) return null; // notification
  try {
    if (method === "initialize") {
      return response(id, {
        result: {
          protocolVersion: params?.protocolVersion ?? "2025-06-18",
          capabilities: { tools: {} },
          serverInfo: { name: "ai-usage", version: VERSION },
          instructions: INSTRUCTIONS,
        },
      });
    }
    if (method === "ping") return response(id, { result: {} });
    if (method === "tools/list") return response(id, { result: { tools: TOOLS } });
    if (method === "tools/call") {
      if (!TOOLS.some((t) => t.name === params?.name)) return response(id, { error: { code: -32602, message: `Unknown tool: ${params?.name}` } });
      try {
        const text = await callTool(params.name, params.arguments);
        return response(id, { result: { content: [{ type: "text", text }] } });
      } catch (err) {
        return response(id, { result: { content: [{ type: "text", text: `ai-usage failed: ${err.message}` }], isError: true } });
      }
    }
    return response(id, { error: { code: -32601, message: `Method not found: ${method}` } });
  } catch (err) {
    return response(id, { error: { code: -32603, message: err.message } });
  }
}

export function serveMcp() {
  const write = (payload) => process.stdout.write(`${JSON.stringify(payload)}\n`);
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on("line", async (line) => {
    if (!line.trim()) return;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      return write(response(null, { error: { code: -32700, message: "Parse error" } }));
    }
    // JSON-RPC batches (MCP 2025-03-26): answer with one array, leaving out notifications.
    if (Array.isArray(msg)) {
      if (msg.length === 0) return write(response(null, { error: { code: -32600, message: "Invalid Request: empty batch" } }));
      const replies = (await Promise.all(msg.map(handle))).filter(Boolean);
      if (replies.length) write(replies);
      return;
    }
    const reply = await handle(msg);
    if (reply) write(reply);
  });
}
