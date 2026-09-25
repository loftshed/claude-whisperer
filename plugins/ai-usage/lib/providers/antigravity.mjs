import { childEnv, resolveBinary, run } from "../exec.mjs";

const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

const WINDOW_MINS = { "5h": 300, weekly: 10080 };

// Families come from the group's model list when present ("Models within this group: Claude Opus, Claude
// Sonnet, GPT-OSS"); the group name "Claude and GPT models" alone would overstate them. GPT-OSS is its own
// family: open-weight models, not the GPT models Codex serves, so "gpt" keeps meaning Codex.
function families(group) {
  const text = (group.description || group.name).toLowerCase();
  const found = ["gemini", "claude"].filter((f) => text.includes(f));
  if (text.includes("gpt-oss")) found.push("gpt-oss");
  if (/gpt(?!-oss)/.test(text)) found.push("gpt");
  return found;
}

function shortGroupName(name) {
  if (/^gemini/i.test(name)) return "Gemini";
  if (/claude.*gpt/i.test(name)) return "Claude & GPT";
  return name.replace(/\s+models?$/i, "");
}

/** Normalises `agy -p /usage --output-format json` (Antigravity CLI 1.1.11+). */
export function parseAntigravityUsage(payload) {
  const groups = payload?.command?.data?.groups;
  if (!Array.isArray(groups) || groups.length === 0) {
    throw new Error(`agy /usage returned no quota groups: ${String(payload?.response ?? "").slice(0, 160)}`);
  }
  const windows = [];
  const pools = [];
  for (const group of groups) {
    const poolId = slug(shortGroupName(group.name));
    const label = shortGroupName(group.name);
    const windowIds = [];
    for (const bucket of group.buckets ?? []) {
      const kind = bucket.window ?? bucket.id;
      const id = `${poolId}-${kind}`;
      const remainingPct = Math.round((bucket.remaining_fraction ?? 0) * 1000) / 10;
      windows.push({
        id,
        label: `${label} ${kind === "5h" ? "5-hour" : kind}`,
        kind,
        windowMins: WINDOW_MINS[kind] ?? null,
        usedPct: Math.round((100 - remainingPct) * 10) / 10,
        remainingPct,
        resetsAt: bucket.reset_time ? Date.parse(bucket.reset_time) : null,
        pool: poolId,
      });
      windowIds.push(id);
    }
    const models = (group.description ?? "").replace(/^Models within this group:\s*/i, "");
    pools.push({ id: poolId, label, families: families(group), models, windowIds });
  }
  return { windows, pools, notes: [] };
}

export async function fetchAntigravity(account) {
  const bin = resolveBinary("agy", account.command);
  const { code, stdout, stderr } = await run(bin, ["-p", "/usage", "--output-format", "json"], {
    env: childEnv(account.env ?? {}),
    timeoutMs: account.timeoutMs ?? 60_000,
    maxBytes: 1024 * 1024,
  });
  let payload;
  try {
    payload = JSON.parse(stdout);
  } catch {
    throw new Error(`agy exited ${code}: ${(stderr || stdout).trim().slice(0, 200)}`);
  }
  if (payload.status && payload.status !== "SUCCESS") throw new Error(`agy /usage status ${payload.status}: ${String(payload.response).slice(0, 160)}`);
  return parseAntigravityUsage(payload);
}
