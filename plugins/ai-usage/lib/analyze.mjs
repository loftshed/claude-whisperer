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

function laneAdvice(lane, now) {
  if (lane.status === "error") return `no data: ${lane.error}`;
  if (lane.status === "blocked") {
    return lane.blockedUntil ? `out until ${formatClock(lane.blockedUntil, now)} (in ${formatDuration(lane.blockedUntil - now)})` : "out (reset time unknown)";
  }
  const parts = [];
  const p = lane.pace;
  if (p) {
    if (p.hoursLeft < 24 && lane.weeklyRemainingPct >= 10) parts.push(`${Math.round(lane.weeklyRemainingPct)}% expires in ${formatDuration(p.hoursLeft * HOUR)}, use it or lose it`);
    else if (p.surplus >= 15) parts.push("under-used, spend freely");
    else if (p.surplus <= -10) parts.push("ahead of pace, conserve");
    else parts.push("on pace");
  }
  if (lane.shortWindow && lane.shortWindow.remainingPct < 20) {
    const w = lane.shortWindow;
    parts.push(`5-hour window ${Math.round(w.remainingPct)}% left${w.resetsAt ? `, refills in ${formatDuration(w.resetsAt - now)}` : ""}`);
  }
  if (lane.stale) parts.push(`data ${formatDuration(now - lane.fetchedAt)} old`);
  return parts.join("; ") || "available";
}

/** Every routable pool across all accounts, ranked best-to-spend first. */
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
      const score = status === "blocked" ? -1000 - (blockedUntil ? (blockedUntil - now) / HOUR : 999) : Math.min(surplus, availableNowPct);
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
  const pools = account.pools ?? [];
  return pools.filter(
    (p) => !pools.some((q) => q !== p && q.windowIds.length < p.windowIds.length && q.windowIds.every((id) => p.windowIds.includes(id))),
  );
}

const gauge = (w) => (w ? { pct: w.remainingPct, level: level(w.remainingPct), label: w.label, resetsAt: w.resetsAt ?? null } : null);

/**
 * One pill per independent pool: its short window (5-hour) and its weekly window, separately. Codex has no
 * short window, so its pill has only the weekly half. Antigravity has two pools, so two pills with tags.
 */
export function pills(account, now = Date.now()) {
  if (!account.windows?.length) return [];
  const windows = new Map(effectiveWindows(account, now).map((w) => [w.id, w]));
  const pools = independentPools(account);
  return pools.map((pool) => {
    const ws = pool.windowIds.map((id) => windows.get(id)).filter(Boolean);
    const short = ws.find((w) => (w.windowMins ?? Infinity) < 1440) ?? null;
    const long = ws.filter((w) => (w.windowMins ?? 0) >= 1440).sort((a, b) => a.remainingPct - b.remainingPct)[0] ?? null;
    return { pool: pool.id, tag: pools.length > 1 ? pool.label.charAt(0).toUpperCase() : null, poolLabel: pool.label, short: gauge(short), weekly: gauge(long) };
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

/** Short name for a window length: "5h", "wk", "1d", "30d". Unknown lengths are "limit". */
export function durationLabel(mins) {
  if (!Number.isFinite(mins) || mins <= 0) return "limit";
  if (mins === 10080) return "wk";
  if (mins % 1440 === 0) return `${mins / 1440}d`;
  if (mins % 60 === 0) return `${mins / 60}h`;
  return `${mins}m`;
}

/**
 * Every independent pool's windows grouped by window length, shortest first: e.g. a "5h" section and a "wk"
 * section. Whatever cycles a provider uses become their own sections, so nothing assumes Claude's 5 hours.
 */
export function sections(accounts, now = Date.now()) {
  const byLength = new Map();
  const unavailable = [];
  for (const account of accounts) {
    if (!account.windows?.length) {
      unavailable.push({ accountId: account.id, label: account.short, error: account.error ?? "no data" });
      continue;
    }
    const windows = new Map(effectiveWindows(account, now).map((w) => [w.id, w]));
    const blocked = blockedWindows(account, now);
    const pools = independentPools(account);
    for (const pool of pools) {
      for (const w of pool.windowIds.map((id) => windows.get(id)).filter(Boolean)) {
        const key = w.windowMins ?? 0;
        if (!byLength.has(key)) byLength.set(key, { windowMins: w.windowMins ?? null, label: durationLabel(w.windowMins), entries: [] });
        byLength.get(key).entries.push({
          accountId: account.id,
          pool: pool.id,
          label: account.short,
          tag: pools.length > 1 ? pool.label.charAt(0).toUpperCase() : null,
          pct: w.remainingPct,
          level: blocked.has(w.id) ? "out" : level(w.remainingPct),
          exhausted: w.remainingPct <= EXHAUSTED_PCT,
          blockedBy: blocked.has(w.id) ? durationLabel(blocked.get(w.id).windowMins) : null,
          resetsAt: w.resetsAt ?? null,
          stale: account.ok === false,
        });
      }
    }
  }
  const list = [...byLength.values()].sort((a, b) => (a.windowMins ?? Infinity) - (b.windowMins ?? Infinity));
  return { sections: list, unavailable };
}

/** Compact per-account figure for status bars: how much can be used right now, per independent pool. */
export function headline(account, now = Date.now()) {
  if (!account.windows?.length) return { text: "?", level: "error" };
  const windows = new Map(effectiveWindows(account, now).map((w) => [w.id, w]));
  const independent = independentPools(account);
  const values = independent.map((p) => Math.min(...p.windowIds.map((id) => windows.get(id)?.remainingPct ?? 100)));
  const best = Math.max(...values);
  return { text: values.map((v) => String(Math.floor(v))).join("/"), level: level(best), values, levels: values.map(level) };
}

export { level };
