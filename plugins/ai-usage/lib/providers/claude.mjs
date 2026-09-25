import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { childEnv, expandHome, resolveBinary, run } from "../exec.mjs";
import { parseResetText } from "../time.mjs";

const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

/**
 * Parses the text Claude Code prints for `/usage`, e.g.
 *   Current session: 42% used · resets Sep 25 at 12:49pm (America/Toronto)
 *   Current week (all models): 11% used · resets Sep 28 at 4:59pm (America/Toronto)
 *   Current week (Fable): 0% used · resets Sep 28 at 5pm (America/Toronto)
 */
export function parseClaudeUsage(text, now = Date.now()) {
  const windows = [];
  const notes = [];
  const scoped = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    const m = line.match(/^Current (session|week)(?:\s*\(([^)]+)\))?\s*:\s*(\d+(?:\.\d+)?)%\s*used\b(.*)$/i);
    if (!m) {
      if (/^extra usage/i.test(line)) notes.push(line);
      continue;
    }
    const [, period, scope, used, rest] = m;
    // Whatever separates "used" from the reset ("·", "-", "(resets …)"), take the text after "resets" and drop
    // one unbalanced closing parenthesis left by the "(resets … (Zone))" form.
    let reset = rest.match(/resets?\s+(.+?)\s*$/i)?.[1];
    if (reset && (reset.match(/\)/g) ?? []).length > (reset.match(/\(/g) ?? []).length) reset = reset.replace(/\)\s*$/, "");
    const usedPct = Number(used);
    const resetsAt = reset ? parseResetText(reset, now) : null;
    if (period.toLowerCase() === "session") {
      windows.push({ id: "5h", label: "5-hour", kind: "5h", windowMins: 300, usedPct, remainingPct: 100 - usedPct, resetsAt });
    } else if (!scope || /^all/i.test(scope)) {
      windows.push({ id: "week", label: "weekly", kind: "weekly", windowMins: 10080, usedPct, remainingPct: 100 - usedPct, resetsAt });
    } else {
      const id = `week-${slug(scope)}`;
      windows.push({ id, label: `${scope} weekly`, kind: "weekly", windowMins: 10080, usedPct, remainingPct: 100 - usedPct, resetsAt, scope });
      scoped.push({ scope, id });
    }
  }
  if (windows.length === 0) {
    if (/total cost/i.test(text)) throw new Error("this profile is on API billing, not a subscription: no quota windows to report");
    throw new Error(`unrecognised /usage output: ${text.slice(0, 160)}`);
  }
  const base = windows.filter((w) => w.id === "5h" || w.id === "week").map((w) => w.id);
  const pools = [{ id: "all", label: "all models", families: ["claude"], windowIds: base }];
  for (const { scope, id } of scoped) {
    pools.push({ id: slug(scope), label: scope, families: ["claude"], models: scope, windowIds: [...base, id] });
  }
  return { windows, pools, notes };
}

const DEFAULT_CONFIG_DIR = join(homedir(), ".claude");

export async function fetchClaude(account, { now = Date.now() } = {}) {
  const bin = resolveBinary("claude", account.command);
  // Claude Code derives the keychain entry name from whether CLAUDE_CONFIG_DIR is set, so the
  // default profile must run with it unset even when the configured dir is ~/.claude.
  const configDir = account.configDir ? resolve(expandHome(account.configDir)) : null;
  const env = childEnv(
    { ...(account.env ?? {}), CLAUDE_CONFIG_DIR: configDir && configDir !== DEFAULT_CONFIG_DIR ? configDir : null },
    { strip: [/^ANTHROPIC_/, /^CLAUDE_CODE_/, "CLAUDECODE", "CLAUDE_CONFIG_DIR"] },
  );
  const args = [
    "-p", "/usage",
    "--output-format", "json",
    "--no-session-persistence",
    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    "--setting-sources", "",
    "--settings", '{"disableAllHooks":true}',
  ];
  // The first claude launch in a fresh app context (e.g. right after the menu bar app is reinstalled) can take
  // well over 15 s; 90 s avoids reporting a cold start as a failure.
  const { code, stdout, stderr } = await run(bin, args, { env, timeoutMs: account.timeoutMs ?? 90_000 });
  let parsed;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    throw new Error(`claude exited ${code}: ${(stderr || stdout).trim().slice(0, 200)}`);
  }
  const result = Array.isArray(parsed) ? parsed.find((m) => m.type === "result") : parsed;
  if (!result || result.is_error) throw new Error(`claude /usage failed: ${String(result?.result ?? stderr).slice(0, 200)}`);
  return { plan: /subscription/i.test(result.result) ? "subscription" : undefined, ...parseClaudeUsage(result.result, now) };
}
