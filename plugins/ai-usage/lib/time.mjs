const MONTHS = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

function zoneParts(utcMs, timeZone) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    second: "numeric",
  }).formatToParts(new Date(utcMs));
  const get = (type) => Number(parts.find((p) => p.type === type).value);
  return { year: get("year"), month: get("month") - 1, day: get("day"), hour: get("hour"), minute: get("minute"), second: get("second") };
}

// Offset of `timeZone` from UTC at the given instant, in milliseconds.
function zoneOffsetMs(utcMs, timeZone) {
  const p = zoneParts(utcMs, timeZone);
  const asUtc = Date.UTC(p.year, p.month, p.day, p.hour, p.minute, p.second);
  return asUtc - Math.floor(utcMs / 1000) * 1000;
}

export function zonedTimeToUtc(year, month, day, hour, minute, timeZone) {
  const wall = Date.UTC(year, month, day, hour, minute);
  const first = wall - zoneOffsetMs(wall, timeZone);
  return wall - zoneOffsetMs(first, timeZone);
}

function isValidZone(timeZone) {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone });
    return true;
  } catch {
    return false;
  }
}

/**
 * Parses Claude Code's human reset text, e.g. "Sep 25 at 8:59pm (America/Toronto)",
 * "Sep 28, 5pm (America/Toronto)" or "3pm (America/Toronto)". The year is never printed,
 * so it is inferred as the nearest occurrence that is not far in the past.
 */
export function parseResetText(text, now = Date.now()) {
  if (!text) return null;
  const tzMatch = text.match(/\(([^()]+)\)\s*$/);
  const timeZone = tzMatch && isValidZone(tzMatch[1].trim()) ? tzMatch[1].trim() : Intl.DateTimeFormat().resolvedOptions().timeZone;
  const body = tzMatch ? text.slice(0, tzMatch.index) : text;
  const m = body.match(/(?:([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?/i);
  if (!m) return null;
  let hour = Number(m[3]);
  const minute = m[4] ? Number(m[4]) : 0;
  const meridiem = m[5]?.toLowerCase();
  if (meridiem === "pm" && hour < 12) hour += 12;
  if (meridiem === "am" && hour === 12) hour = 0;

  const today = zoneParts(now, timeZone);
  if (m[1] && MONTHS[m[1].toLowerCase()] !== undefined) {
    const month = MONTHS[m[1].toLowerCase()];
    const day = Number(m[2]);
    const candidates = [today.year - 1, today.year, today.year + 1].map((y) => zonedTimeToUtc(y, month, day, hour, minute, timeZone));
    return candidates.find((t) => t >= now - DAY) ?? candidates.at(-1);
  }
  let t = zonedTimeToUtc(today.year, today.month, today.day, hour, minute, timeZone);
  if (t < now - 60_000) t += DAY;
  return t;
}

export function formatDuration(ms) {
  if (ms <= 0) return "now";
  const mins = Math.round(ms / 60_000);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ${String(mins % 60).padStart(2, "0")}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

export function formatClock(ms, now = Date.now()) {
  const d = new Date(ms);
  const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  const sameDay = new Date(now).toDateString() === d.toDateString();
  if (sameDay) return `${time} today`;
  if (ms - now < 7 * DAY && ms > now) return `${d.toLocaleDateString("en-US", { weekday: "short" })} ${time}`;
  return `${d.toLocaleDateString("en-US", { month: "short", day: "numeric" })} ${time}`;
}

export { HOUR, DAY };
