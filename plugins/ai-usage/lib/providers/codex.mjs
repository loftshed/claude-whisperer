import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { childEnv, resolveBinary } from "../exec.mjs";
import { VERSION } from "../version.mjs";

function windowKind(mins) {
  if (mins === 300) return { kind: "5h", label: "5-hour" };
  if (mins === 10080) return { kind: "weekly", label: "weekly" };
  if (mins % 1440 === 0) return { kind: `${mins / 1440}d`, label: `${mins / 1440}-day` };
  return { kind: `${Math.round(mins / 60)}h`, label: `${Math.round(mins / 60)}-hour` };
}

/** Normalises the `account/rateLimits/read` result from `codex app-server`. */
export function parseCodexRateLimits(result) {
  const byId = result.rateLimitsByLimitId ?? (result.rateLimits ? { [result.rateLimits.limitId ?? "codex"]: result.rateLimits } : {});
  const windows = [];
  const pools = [];
  const notes = [];
  let plan;
  for (const [limitId, limit] of Object.entries(byId)) {
    plan ??= limit.planType;
    const windowIds = [];
    for (const w of [limit.primary, limit.secondary]) {
      if (!w) continue;
      const { kind, label } = windowKind(w.windowDurationMins);
      const id = limitId === "codex" ? kind : `${limitId}-${kind}`;
      windows.push({
        id,
        label: limitId === "codex" ? label : `${limit.limitName ?? limitId} ${label}`,
        kind,
        windowMins: w.windowDurationMins,
        usedPct: w.usedPercent,
        remainingPct: 100 - w.usedPercent,
        resetsAt: w.resetsAt ? w.resetsAt * 1000 : null,
      });
      windowIds.push(id);
    }
    pools.push({ id: limitId, label: limitId === "codex" ? "Codex models" : (limit.limitName ?? limitId), families: ["gpt"], windowIds });
    if (limit.rateLimitReachedType) notes.push(`limit reached: ${limit.rateLimitReachedType}`);
    if (limit.spendControlReached) notes.push("spend control reached");
    const credits = limit.credits;
    if (limitId === "codex" && credits && (credits.hasCredits || credits.unlimited)) {
      notes.push(credits.unlimited ? "credits: unlimited" : `credits balance: ${credits.balance}`);
    }
  }
  if (result.ordinaryUsageAllowed === false) notes.push("ordinary usage not allowed right now");
  if (windows.length === 0) throw new Error("codex reported no rate-limit windows (not signed in with ChatGPT?)");
  return { plan, windows, pools, notes };
}

// Talks JSON-RPC to `codex app-server` over stdio; Codex authenticates with its own stored login.
function rpcRateLimits(bin, env, timeoutMs) {
  return new Promise((resolve, reject) => {
    const child = spawn(bin, ["app-server"], { env, cwd: homedir(), stdio: ["pipe", "pipe", "pipe"] });
    let buf = "";
    let stderr = "";
    let settled = false;
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.kill();
      fn(value);
    };
    const timer = setTimeout(() => finish(reject, new Error(`codex app-server timed out after ${timeoutMs / 1000}s`)), timeoutMs);
    const send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
    child.stdout.on("data", (d) => {
      buf += d;
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i);
        buf = buf.slice(i + 1);
        let msg;
        try {
          msg = JSON.parse(line);
        } catch {
          continue;
        }
        if (msg.id === 1) {
          if (msg.error) return finish(reject, new Error(`codex initialize: ${msg.error.message}`));
          send({ method: "initialized" });
          send({ id: 2, method: "account/rateLimits/read" });
        } else if (msg.id === 2) {
          if (msg.error) return finish(reject, new Error(`codex rateLimits: ${msg.error.message}`));
          finish(resolve, msg.result);
        }
      }
    });
    child.stderr.on("data", (d) => {
      if (stderr.length < 16_384) stderr += d;
    });
    child.on("error", (err) => finish(reject, err));
    child.on("close", (code) => finish(reject, new Error(`codex app-server exited ${code}: ${stderr.trim().slice(0, 200)}`)));
    send({ id: 1, method: "initialize", params: { clientInfo: { name: "ai-usage", version: VERSION } } });
  });
}

export async function fetchCodex(account) {
  const bin = resolveBinary("codex", account.command);
  const env = childEnv(account.env ?? {});
  const result = await rpcRateLimits(bin, env, account.timeoutMs ?? 30_000);
  return parseCodexRateLimits(result);
}
