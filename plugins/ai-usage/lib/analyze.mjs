import { formatClock, formatDuration, HOUR } from "./time.mjs";

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const EXHAUSTED_PCT = 0.5;

/** A window whose reset time has passed since it was read is full again; its next reset is unknown. */
export function effectiveWindow(w, now = Date.now()) {
  if (w.resetsAt && w.resetsAt <= now) return { ...w, usedPct: 0, remainingPct: 100, resetsAt: null, resetSinceFetch: true };
  return w;
}

export function effectiveWindows(account, now = Date.now()) {
  return (account.windows ?? []).map((w) => effectiveWindow(w, now));
}

function level(pct) {
  if (pct <= EXHAUSTED_PCT) return "out";
  if (pct < 20) return "low";
  if (pct < 50) return "mid";
  return "ok";
}

/**
 * Pace compares what is left with what would be left if the window were used evenly until it resets.
 * surplus > 0 means the account is under-used: that capacity is lost at reset unless it is spent.
 */
function pace(w, now) {
  if (!w.resetsAt || !w.windowMins) return null;
  const hoursLeft = Math.max(0, (w.resetsAt - now) / HOUR);
  const evenLeft = clamp((100 * hoursLeft) / (w.windowMins / 60), 0, 100);
  return {
    windowId: w.id,
    hoursLeft,
    evenLeftPct: evenLeft,
    surplus: w.remainingPct - evenLeft,
    burnPctPerHour: w.remainingPct / Math.max(hoursLeft, 0.25),
  };
}

// Below this much usable right now a pool can barely take a task, whatever its weekly surplus.
const LOW_ROOM_PCT = 10;
// A day-or-longer limit is expiring in the last 20% of its window (about 34 h of a week) with at least 5%
// left: that remainder is lost at the rollover unless it is spent, so it goes first.
const EXPIRING_FRACTION = 0.2;
const EXPIRING_MIN_PCT = 5;

export function isExpiring(w, now = Date.now()) {
  if (!w.resetsAt || !w.windowMins || w.windowMins < 1440 || w.remainingPct < EXPIRING_MIN_PCT) return false;
  const hoursLeft = (w.resetsAt - now) / HOUR;
  return hoursLeft > 0 && hoursLeft <= (w.windowMins / 60) * EXPIRING_FRACTION;
}

const rate = (pctPerHour) => (pctPerHour >= 10 ? Math.round(pctPerHour) : Math.round(pctPerHour * 10) / 10);

function laneAdvice(lane, now) {
  if (lane.status === "error") return `no data: ${lane.error}`;
  if (lane.status === "blocked") {
    return lane.blockedUntil ? `out until ${formatClock(lane.blockedUntil, now)} (in ${formatDuration(lane.blockedUntil - now)})` : "out (reset time unknown)";
  }
  const parts = [];
  const e = lane.expiringWindow;
  if (e) {
    parts.push(`use it or lose it: ${Math.floor(e.remainingPct)}% of the ${durationName(e.windowMins)} limit resets ${formatClock(e.resetsAt, now)} (in ${formatDuration(e.resetsAt - now)}); ~${rate(lane.burnPctPerHour)}%/h uses it all`);
  } else if (lane.pace) {
    if (lane.pace.surplus >= 15) parts.push("under-used, spend freely");
    else if (lane.pace.surplus <= -10) parts.push("ahead of pace, conserve");
    else parts.push("on pace");
  }
  const w = lane.shortWindow;
  if (lane.lowRoom) {
    const refill = w && w.remainingPct < LOW_ROOM_PCT && w.resetsAt ? `, refills in ${formatDuration(w.resetsAt - now)}` : "";
    parts.push(`only ${Math.floor(lane.availableNowPct)}% usable now: small tasks only${refill}`);
  } else if (w && w.remainingPct < 20) {
    parts.push(`${durationName(w.windowMins)} window ${Math.round(w.remainingPct)}% left${w.resetsAt ? `, refills in ${formatDuration(w.resetsAt - now)}` : ""}`);
  }
  if (lane.stale) parts.push(`data ${formatDuration(now - lane.fetchedAt)} old`);
  return parts.join("; ") || "available";
}

/**
 * Every routable pool across all accounts, best to spend first:
 *   1. expiring pools with room, most urgent (highest %/h needed to use it all) first;
 *   2. other pools with room, by min(surplus vs even pace, % usable now);
 *   3. pools with under 10% usable now; 4. blocked pools, soonest back first; 5. pools with no data.
 */
export function buildLanes(accounts, now = Date.now()) {
  const lanes = [];
  for (const account of accounts) {
    const base = { accountId: account.id, accountLabel: account.label, provider: account.provider, fetchedAt: account.fetchedAt ?? null };
    if (!account.windows?.length) {
      lanes.push({ ...base, poolId: null, label: account.label, families: [], status: "error", error: account.error ?? "no data", score: -2000 });
      continue;
    }
    const windows = new Map(effectiveWindows(account, now).map((w) => [w.id, w]));
    for (const pool of account.pools ?? []) {
      const ws = pool.windowIds.map((id) => windows.get(id)).filter(Boolean);
      if (ws.length === 0) continue;
      const availableNowPct = Math.min(...ws.map((w) => w.remainingPct));
      const exhausted = ws.filter((w) => w.remainingPct <= EXHAUSTED_PCT);
      const blockedUntil = exhausted.length ? Math.max(...exhausted.map((w) => w.resetsAt ?? 0)) || null : null;
      const long = ws.filter((w) => (w.windowMins ?? 0) >= 1440);
      const paces = long.map((w) => pace(w, now)).filter(Boolean).sort((a, b) => a.surplus - b.surplus);
      const binding = paces[0] ?? null;
      const weeklyRemainingPct = long.length ? Math.min(...long.map((w) => w.remainingPct)) : availableNowPct;
      const shortWindow = ws.find((w) => (w.windowMins ?? Infinity) < 1440) ?? null;
      const stale = account.ok === false || (account.fetchedAt && now - account.fetchedAt > HOUR);
      const status = exhausted.length ? "blocked" : "available";
      const surplus = binding?.surplus ?? 0;
      const expiringWindow = status === "available" ? long.filter((w) => isExpiring(w, now)).sort((a, b) => a.resetsAt - b.resetsAt)[0] ?? null : null;
      const burnPctPerHour = expiringWindow ? Math.min(expiringWindow.remainingPct, weeklyRemainingPct) / Math.max((expiringWindow.resetsAt - now) / HOUR, 0.25) : null;
      const lowRoom = status === "available" && availableNowPct < LOW_ROOM_PCT;
      let score;
      if (status === "blocked") score = -1000 - (blockedUntil ? (blockedUntil - now) / HOUR : 999);
      else if (lowRoom) score = -500 + Math.min(surplus, availableNowPct);
      else if (expiringWindow) score = 1000 + burnPctPerHour;
      else score = Math.min(surplus, availableNowPct);
      const lane = {
        ...base,
        poolId: pool.id,
        label: account.pools.length > 1 ? `${account.label} · ${pool.label}` : account.label,
        models: pool.models,
        families: pool.families ?? [],
        route: account.routes?.[pool.id] ?? account.route ?? null,
        status,
        availableNowPct,
        weeklyRemainingPct,
        blockedUntil,
        pace: binding,
        shortWindow,
        expiringWindow,
        expiring: Boolean(expiringWindow),
        burnPctPerHour,
        lowRoom,
        stale: Boolean(stale),
        score,
      };
      lane.advice = laneAdvice(lane, now);
      lanes.push(lane);
    }
    markRedundantSubPools(lanes.filter((l) => l.accountId === account.id), account.pools ?? []);
  }
  return lanes.sort((a, b) => b.score - a.score);
}

// A per-model sub-limit (Claude's "Fable weekly") only deserves its own row when it binds harder than its parent pool.
function markRedundantSubPools(lanes, pools) {
  for (const pool of pools) {
    const parent = pools.find((q) => q !== pool && q.windowIds.length < pool.windowIds.length && q.windowIds.every((id) => pool.windowIds.includes(id)));
    if (!parent) continue;
    const lane = lanes.find((l) => l.poolId === pool.id);
    const parentLane = lanes.find((l) => l.poolId === parent.id);
    if (!lane || !parentLane) continue;
    lane.subPoolOf = parent.id;
    lane.redundant = lane.status === parentLane.status && Math.abs(lane.score - parentLane.score) < 1 && Math.abs(lane.availableNowPct - parentLane.availableNowPct) < 1;
  }
}

// A pool whose windows are a strict superset of another pool's (e.g. Claude's per-model weekly cap) is a
// sub-limit of that pool; compact views show only the independent pools.
function independentPools(account) {
  const pools = (account.pools ?? []).filter((p) => p.windowIds.length > 0);
  return pools.filter(
    (p) => !pools.some((q) => q !== p && q.windowIds.length < p.windowIds.length && q.windowIds.every((id) => p.windowIds.includes(id))),
  );
}

const gauge = (w, now) =>
  w
    ? {
        pct: w.remainingPct,
        level: level(w.remainingPct),
        label: w.label,
        windowMins: w.windowMins ?? null,
        resetsAt: w.resetsAt ?? null,
        exhausted: w.remainingPct <= EXHAUSTED_PCT,
        expiring: isExpiring(w, now),
      }
    : null;

/**
 * Each independent pool's short window (5-hour) and weekly (longest) window. The menu bar draws one pill per
 * provider from these: Codex has no short window; Antigravity has two pools, told apart by their tags.
 */
export function pills(account, now = Date.now()) {
  if (!account.windows?.length) return [];
  const windows = new Map(effectiveWindows(account, now).map((w) => [w.id, w]));
  const pools = independentPools(account);
  return pools.map((pool) => {
    const ws = pool.windowIds.map((id) => windows.get(id)).filter(Boolean);
    const short = ws.find((w) => (w.windowMins ?? Infinity) < 1440) ?? null;
    const long = ws.filter((w) => (w.windowMins ?? 0) >= 1440).sort((a, b) => a.remainingPct - b.remainingPct)[0] ?? null;
    return { pool: pool.id, tag: pools.length > 1 ? pool.label.charAt(0).toUpperCase() : null, poolLabel: pool.label, short: gauge(short, now), weekly: gauge(long, now) };
  });
}

/**
 * Windows that cannot be used because a longer window of the same pool is exhausted: 5-hour allowance is
 * worthless once the week is gone. Only longer windows block shorter ones; an empty 5-hour window does not
 * make the weekly remainder any less real. Returns Map(windowId -> blocking window).
 */
export function blockedWindows(account, now = Date.now()) {
  const windows = new Map(effectiveWindows(account, now).map((w) => [w.id, w]));
  const blocked = new Map();
  for (const pool of independentPools(account)) {
    const ws = pool.windowIds.map((id) => windows.get(id)).filter(Boolean);
    for (const w of ws) {
      const blocker = ws.find((o) => o !== w && (o.windowMins ?? 0) > (w.windowMins ?? 0) && o.remainingPct <= EXHAUSTED_PCT);
      if (blocker && !blocked.has(w.id)) blocked.set(w.id, blocker);
    }
  }
  // A per-model cap (Claude's "Fable weekly") is unusable once its parent pool's limit of the same or a
  // longer length is exhausted; the reverse does not hold.
  const pools = account.pools ?? [];
  for (const sub of pools) {
    const parent = pools.find((q) => q !== sub && q.windowIds.length < sub.windowIds.length && q.windowIds.every((id) => sub.windowIds.includes(id)));
    if (!parent) continue;
    for (const id of sub.windowIds.filter((wid) => !parent.windowIds.includes(wid))) {
      const w = windows.get(id);
      const blocker = w && parent.windowIds.map((pid) => windows.get(pid)).find((o) => o && (o.windowMins ?? 0) >= (w.windowMins ?? 0) && o.remainingPct <= EXHAUSTED_PCT);
      if (blocker && !blocked.has(id)) blocked.set(id, blocker);
    }
  }
  return blocked;
}

/** Name for a window length in prose: "weekly", "daily", "5-hour", else the short label. */
export function durationName(mins) {
  if (mins === 10080) return "weekly";
  if (mins === 1440) return "daily";
  if (Number.isFinite(mins) && mins > 0 && mins < 1440 && mins % 60 === 0) return `${mins / 60}-hour`;
  return durationLabel(mins);
}

/** Short name for a window length: "5h", "wk", "1d", "30d". Unknown lengths are "limit". */
export function durationLabel(mins) {
  if (!Number.isFinite(mins) || mins <= 0) return "limit";
  if (mins === 10080) return "wk";
  if (mins % 1440 === 0) return `${mins / 1440}d`;
  if (mins % 60 === 0) return `${mins / 60}h`;
  return `${mins}m`;
}

/**
 * The menu bar layout. Every usable pool's windows grouped by window length, shortest first (e.g. "5h" and
 * "wk" sections; whatever cycles a provider uses become their own sections). A pool whose day-or-longer
 * limit is used up is done for that period: it leaves the sections and is listed once under `exhausted`.
 */
export function sections(accounts, now = Date.now()) {
  const byLength = new Map();
  const exhausted = [];
  const unavailable = [];
  for (const account of accounts) {
    if (!account.windows?.length) {
      unavailable.push({ accountId: account.id, label: account.short, error: account.error ?? "no data" });
      continue;
    }
    const windows = new Map(effectiveWindows(account, now).map((w) => [w.id, w]));
    const pools = independentPools(account);
    for (const pool of pools) {
      const ws = pool.windowIds.map((id) => windows.get(id)).filter(Boolean);
      const base = {
        accountId: account.id,
        pool: pool.id,
        label: account.short,
        tag: pools.length > 1 ? pool.label.charAt(0).toUpperCase() : null,
        stale: account.ok === false,
      };
      const spent = ws
        .filter((w) => (w.windowMins ?? 0) >= 1440 && w.remainingPct <= EXHAUSTED_PCT)
        .sort((a, b) => (b.resetsAt ?? 0) - (a.resetsAt ?? 0))[0];
      if (spent) {
        exhausted.push({ ...base, window: durationLabel(spent.windowMins), resetsAt: spent.resetsAt ?? null });
        continue;
      }
      for (const w of ws) {
        const key = w.windowMins ?? 0;
        if (!byLength.has(key)) byLength.set(key, { windowMins: w.windowMins ?? null, label: durationLabel(w.windowMins), entries: [] });
        byLength.get(key).entries.push({
          ...base,
          pct: w.remainingPct,
          level: level(w.remainingPct),
          exhausted: w.remainingPct <= EXHAUSTED_PCT,
          expiring: isExpiring(w, now),
          resetsAt: w.resetsAt ?? null,
        });
      }
    }
  }
  const list = [...byLength.values()].sort((a, b) => (a.windowMins ?? Infinity) - (b.windowMins ?? Infinity));
  return { sections: list, exhausted, unavailable };
}

/** Compact per-account figure for status bars: how much can be used right now, per independent pool. */
export function headline(account, now = Date.now()) {
  if (!account.windows?.length) return { text: "?", level: "error" };
  const windows = new Map(effectiveWindows(account, now).map((w) => [w.id, w]));
  const independent = independentPools(account);
  if (independent.length === 0) return { text: "?", level: "error" };
  const values = independent.map((p) => Math.min(...p.windowIds.map((id) => windows.get(id)?.remainingPct ?? 100)));
  const best = Math.max(...values);
  return { text: values.map((v) => String(Math.floor(v))).join("/"), level: level(best), values, levels: values.map(level) };
}

export { level };
