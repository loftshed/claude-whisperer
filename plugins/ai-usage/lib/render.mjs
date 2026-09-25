import { blockedWindows, buildLanes, durationLabel, effectiveWindows, headline, level, pills, sections } from "./analyze.mjs";
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
    const blocked = blockedWindows(account, now);
    for (const w of effectiveWindows(account, now)) {
      const blocker = blocked.get(w.id);
      const lv = blocker ? "out" : level(w.remainingPct);
      const dead = blocker || w.remainingPct <= 0.5;
      const pct = padStart(dead ? SKULL : `${Math.floor(w.remainingPct)}%`, 4);
      const reset = blocker
        ? c("dim", `unusable until the ${durationLabel(blocker.windowMins)} limit resets`)
        : w.resetSinceFetch
        ? c("dim", "reset since last check")
        : w.resetsAt
          ? `${c("dim", "resets")} ${formatClock(w.resetsAt, now)} ${c("dim", `(in ${formatDuration(w.resetsAt - now)})`)}`
          : c("dim", "not started");
      out.push(`   ${pad(w.label, 22)} ${c(LEVEL_COLOR[lv], bar(blocker ? 0 : w.remainingPct, 14))} ${c(LEVEL_COLOR[lv], pct)} ${dead ? "    " : "left"}   ${reset}`);
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

// Marks limits that are used up: the ☠ group in the menu bar and `line`, and dead rows in the terminal view.
export const SKULL = "☠";

// Marks the pool that is expiring: near its weekly rollover with capacity left.
export const HOURGLASS = "⏳";

/** Time until a rollover in the largest whole unit: "3d", "5h", "40m". */
export function timeLeft(resetsAt, now = Date.now()) {
  if (!resetsAt) return "";
  const minutes = Math.floor((resetsAt - now) / 60_000);
  if (minutes <= 0) return "now";
  if (minutes >= 1440) return `${Math.floor(minutes / 1440)}d`;
  if (minutes >= 60) return `${Math.floor(minutes / 60)}h`;
  return `${minutes}m`;
}

/**
 * One provider's pill as text: "16·78 3d" (5-hour % left · weekly % left, then time to the weekly rollover),
 * "49 4d" when there is no 5-hour limit, "☠5h" when the week is used up (then time until it is back).
 * Antigravity's pools are tagged: "G76·3 4h C100·27 1d⏳".
 */
export function pillText(account, now = Date.now()) {
  const list = pills(account, now);
  if (list.length === 0) return "?";
  const pct = (g) => String(Math.floor(g.pct));
  const pools = list.map((p) => {
    const tag = p.tag ?? "";
    if (p.weekly?.exhausted) return `${tag}${SKULL}${timeLeft(p.weekly.resetsAt, now)}`;
    const weekly = p.weekly ? `${pct(p.weekly)} ${timeLeft(p.weekly.resetsAt, now)}`.trim() + (p.weekly.expiring ? HOURGLASS : "") : "";
    return tag + [p.short && pct(p.short), weekly].filter(Boolean).join("·");
  });
  return pools.join(" ") + (account.ok === false ? "!" : "");
}

/** One line for tmux or a status line, one group per provider: "CW ☠5h · CP 16·78 3d · CX 49 4d · AG …". */
export function renderLine(accounts, now = Date.now()) {
  return accounts.map((a) => `${a.short} ${pillText(a, now)}`).join(" · ");
}

const iso = (ms) => (ms ? new Date(ms).toISOString() : null);
const minutesUntil = (ms, now) => (ms ? Math.max(0, Math.round((ms - now) / 60_000)) : null);

/** Stable machine-readable view shared by `ai-usage json`, the menu bar app and the MCP server. */
export function jsonView(accounts, now = Date.now()) {
  return {
    schema: SNAPSHOT_SCHEMA,
    version: VERSION,
    generatedAt: iso(now),
    // The menu bar layout: one section per window length (e.g. "5h", "wk"), shortest first.
    ...(({ sections: list, exhausted, unavailable }) => ({
      sections: list.map((sec) => ({ ...sec, entries: sec.entries.map((e) => ({ ...e, resetsAt: iso(e.resetsAt) })) })),
      exhausted: exhausted.map((e) => ({ ...e, resetsAt: iso(e.resetsAt) })),
      unavailable,
    }))(sections(accounts, now)),
    accounts: accounts.map((a) => ({
      id: a.id,
      label: a.label,
      short: a.short,
      provider: a.provider,
      plan: a.plan ?? null,
      billing: a.billing ?? null,
      ok: a.ok !== false,
      error: a.ok === false ? a.error : null,
      fetchedAt: iso(a.fetchedAt),
      ageMinutes: a.fetchedAt ? Math.round((now - a.fetchedAt) / 60_000) : null,
      headline: headline(a, now),
      pills: pills(a, now).map((p) => ({
        ...p,
        short: p.short && { ...p.short, resetsAt: iso(p.short.resetsAt) },
        weekly: p.weekly && { ...p.weekly, resetsAt: iso(p.weekly.resetsAt) },
      })),
      windows: ((blocked) => effectiveWindows(a, now).map((w) => ({
        id: w.id,
        label: w.label,
        kind: w.kind,
        usedPct: w.usedPct,
        remainingPct: w.remainingPct,
        resetsAt: iso(w.resetsAt),
        resetsInMinutes: minutesUntil(w.resetsAt, now),
        resetSinceFetch: Boolean(w.resetSinceFetch),
        exhausted: w.remainingPct <= 0.5,
        blockedBy: blocked.has(w.id) ? durationLabel(blocked.get(w.id).windowMins) : null,
      })))(blockedWindows(a, now)),
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
      expiring: Boolean(l.expiring),
      expiresAt: iso(l.expiringWindow?.resetsAt),
      burnPctPerHour: l.burnPctPerHour != null ? Math.round(l.burnPctPerHour * 10) / 10 : null,
      lowRoom: Boolean(l.lowRoom),
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
