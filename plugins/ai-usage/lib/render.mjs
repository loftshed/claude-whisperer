import { buildLanes, effectiveWindows, headline, level } from "./analyze.mjs";
import { formatClock, formatDuration } from "./time.mjs";
import { SNAPSHOT_SCHEMA, VERSION } from "./version.mjs";

const ANSI = { reset: "\x1b[0m", bold: "\x1b[1m", dim: "\x1b[2m", red: "\x1b[31m", green: "\x1b[32m", yellow: "\x1b[33m", cyan: "\x1b[36m" };
const LEVEL_COLOR = { ok: "green", mid: "yellow", low: "red", out: "red", error: "red" };

export function useColor(stream = process.stdout) {
  return Boolean(stream.isTTY) && !process.env.NO_COLOR && process.env.TERM !== "dumb";
}

function painter(color) {
  return (style, text) => (color ? `${style.split(" ").map((s) => ANSI[s]).join("")}${text}${ANSI.reset}` : text);
}

const visibleLength = (s) => s.replace(/\x1b\[[0-9;]*m/g, "").length;
const pad = (s, n) => s + " ".repeat(Math.max(0, n - visibleLength(s)));
const padStart = (s, n) => " ".repeat(Math.max(0, n - visibleLength(s))) + s;

export const surplusText = (surplus) => `${surplus >= 0 ? "+" : "−"}${Math.round(Math.abs(surplus))} pts`;

function bar(pct, width) {
  const eighths = Math.round((Math.max(0, Math.min(100, pct)) / 100) * width * 8);
  const full = Math.floor(eighths / 8);
  const part = eighths % 8;
  return "█".repeat(full) + (part ? "▏▎▍▌▋▊▉"[part - 1] : "") + "·".repeat(width - full - (part ? 1 : 0));
}

export function renderReport(accounts, { color = false, now = Date.now(), refreshing = false, footer = "" } = {}) {
  const c = painter(color);
  const out = [];
  const oldest = Math.min(...accounts.map((a) => a.fetchedAt ?? now));
  const status = refreshing ? c("cyan", "refreshing…") : c("dim", `data ${formatDuration(now - oldest)} old`);
  out.push(`${c("bold", " AI usage")}  ${c("dim", new Date(now).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }))}  ${status}`);
  out.push("");

  for (const account of accounts) {
    const h = headline(account, now);
    const plan = account.plan ? c("dim", `  ${account.plan}`) : "";
    out.push(` ${c(`bold ${LEVEL_COLOR[h.level]}`, account.label)}${plan}`);
    if (account.ok === false) {
      const when = account.errorAt ? ` ${formatDuration(now - account.errorAt)} ago` : "";
      out.push(`   ${c("red", `⚠ refresh failed${when}: ${account.error}`)}`);
      if (account.fetchedAt) out.push(`   ${c("dim", `showing last good data from ${formatDuration(now - account.fetchedAt)} ago`)}`);
    }
    for (const w of effectiveWindows(account, now)) {
      const lv = level(w.remainingPct);
      const pct = padStart(`${Math.floor(w.remainingPct)}%`, 4);
      const reset = w.resetSinceFetch
        ? c("dim", "reset since last check")
        : w.resetsAt
          ? `${c("dim", "resets")} ${formatClock(w.resetsAt, now)} ${c("dim", `(in ${formatDuration(w.resetsAt - now)})`)}`
          : c("dim", "not started");
      out.push(`   ${pad(w.label, 22)} ${c(LEVEL_COLOR[lv], bar(w.remainingPct, 14))} ${c(LEVEL_COLOR[lv], pct)} left   ${reset}`);
    }
    for (const note of account.notes ?? []) out.push(`   ${c("dim", note)}`);
    out.push("");
  }

  const lanes = buildLanes(accounts, now);
  out.push(` ${c("bold", "Where to spend next")}  ${c("dim", "(pts = weekly % left minus what even use would leave)")}`);
  let rank = 0;
  for (const lane of lanes.filter((l) => !l.redundant)) {
    const available = lane.status === "available";
    const marker = available ? padStart(String(++rank), 2) : c("red", " ✗");
    const pts = available && lane.pace ? surplusText(lane.pace.surplus) : "";
    const ptsColor = !pts ? "dim" : lane.pace.surplus >= 15 ? "green" : lane.pace.surplus <= -10 ? "yellow" : "dim";
    const route = lane.route ? c("cyan", lane.route) : "";
    out.push(` ${marker}  ${pad(lane.label, 34)} ${c(ptsColor, padStart(pts, 8))}   ${pad(lane.advice ?? "", 44)} ${route}`);
  }
  if (footer) out.push("", c("dim", ` ${footer}`));
  return out.join("\n");
}

export function renderLine(accounts, now = Date.now()) {
  return accounts.map((a) => `${a.short} ${headline(a, now).text}`).join(" · ");
}

const iso = (ms) => (ms ? new Date(ms).toISOString() : null);
const minutesUntil = (ms, now) => (ms ? Math.max(0, Math.round((ms - now) / 60_000)) : null);

/** Stable machine-readable view shared by `ai-usage json`, the menu bar app and the MCP server. */
export function jsonView(accounts, now = Date.now()) {
  return {
    schema: SNAPSHOT_SCHEMA,
    version: VERSION,
    generatedAt: iso(now),
    accounts: accounts.map((a) => ({
      id: a.id,
      label: a.label,
      short: a.short,
      provider: a.provider,
      plan: a.plan ?? null,
      ok: a.ok !== false,
      error: a.ok === false ? a.error : null,
      fetchedAt: iso(a.fetchedAt),
      ageMinutes: a.fetchedAt ? Math.round((now - a.fetchedAt) / 60_000) : null,
      headline: headline(a, now),
      windows: effectiveWindows(a, now).map((w) => ({
        id: w.id,
        label: w.label,
        kind: w.kind,
        usedPct: w.usedPct,
        remainingPct: w.remainingPct,
        resetsAt: iso(w.resetsAt),
        resetsInMinutes: minutesUntil(w.resetsAt, now),
        resetSinceFetch: Boolean(w.resetSinceFetch),
      })),
      notes: a.notes ?? [],
    })),
    lanes: buildLanes(accounts, now).map((l) => ({
      label: l.label,
      accountId: l.accountId,
      poolId: l.poolId,
      families: l.families,
      models: l.models ?? null,
      status: l.status,
      availableNowPct: l.availableNowPct ?? 0,
      weeklyRemainingPct: l.weeklyRemainingPct ?? 0,
      surplusPts: l.pace ? Math.round(l.pace.surplus) : null,
      weeklyResetsInHours: l.pace ? Math.round(l.pace.hoursLeft * 10) / 10 : null,
      blockedUntil: iso(l.blockedUntil),
      route: l.route ?? null,
      advice: l.advice,
      subPoolOf: l.subPoolOf ?? null,
      redundant: Boolean(l.redundant),
      stale: l.stale,
      score: Math.round(l.score * 10) / 10,
    })),
  };
}
