import { formatClock } from "./time.mjs";

const DAY = 86_400_000;

/** Observations, not attribution: a changed allowance cannot identify a manual or global reset. */
export function observeResets(data, previous, now) {
  const history = (previous?.resetHistory ?? []).filter(
    (event) => now - event.observedAt < 30 * DAY,
  );
  const windows = data.windows ?? [];
  for (const window of windows) {
    if (window.windowMins < 1440) continue;
    const before = previous?.windows?.find((w) => w.id === window.id);
    if (!before || !before.resetsAt || before.resetsAt <= now || !window.resetsAt) continue;
    // Ignore display rounding and ordinary rolling recovery. Keep only changes before the old deadline.
    const moved = window.resetsAt && Math.abs(window.resetsAt - before.resetsAt) > 30 * 60_000;
    const isRefilled = window.remainingPct - before.remainingPct >= 20;
    if (!moved && !isRefilled) continue;
    // A full idle window can acquire a later deadline when use starts. That is not a refill.
    if (before.remainingPct >= 99.5 && !isRefilled && window.resetsAt > before.resetsAt) continue;
    history.push({
      windowId: window.id,
      label: window.label,
      observedAt: now,
      previousResetsAt: before.resetsAt,
      resetsAt: window.resetsAt,
      previousRemainingPct: before.remainingPct,
      remainingPct: window.remainingPct,
      scheduleChanged: Boolean(moved),
    });
  }
  return history.slice(-20);
}

/** Explicitly configured notices are informational. They never change quota or spending deadlines. */
export function resetAnnouncements(account, now) {
  const notices = Array.isArray(account.resetAnnouncements) ? account.resetAnnouncements : [];
  return notices
    .filter((notice) => {
      if (
        !notice ||
        typeof notice.title !== "string" ||
        !notice.title.trim() ||
        typeof notice.at !== "string" ||
        !/(?:Z|[+-]\d{2}:\d{2})$/u.test(notice.at)
      )
        return false;
      const at = Date.parse(notice.at);
      if (!Number.isFinite(at) || at <= now) return false;
      try {
        return new URL(notice.url).protocol === "https:";
      } catch {
        return false;
      }
    })
    .slice(0, 10)
    .map(({ title, at, url }) => ({ title, at: new Date(at).toISOString(), url }));
}

/** Shared wording for the terminal, native app and agent view. */
export function resetDetails(account, now) {
  const details = [];
  const bank = account.resetCredits;
  if (bank) {
    const suffix = account.ok === false ? " (last reported)" : "";
    details.push(
      `${bank.availableCount} banked reset${bank.availableCount === 1 ? "" : "s"} available${suffix}; separate from the weekly rollover`,
    );
    const expiry = bank.credits
      ?.filter((c) => c.status === "available" && c.expiresAt > now)
      .map((c) => c.expiresAt)
      .sort((a, b) => a - b)[0];
    if (expiry) details.push(`Next reported banked reset expiry: ${formatClock(expiry, now)}`);
  }
  const recent = (account.resetHistory ?? [])
    .filter((e) => now - e.observedAt < 30 * DAY)
    .slice(-3);
  for (const event of recent) {
    const when = formatClock(event.observedAt, now);
    if (event.scheduleChanged) {
      details.push(`${event.label}: rollover changed, observed ${when}`);
      details.push(
        `Previously ${formatClock(event.previousResetsAt, now)}; then reported ${formatClock(event.resetsAt, now)}`,
      );
      details.push("Previous dates are history, not confirmed extra refills.");
    } else {
      details.push(
        `${event.label}: remaining rose ${Math.round(event.previousRemainingPct)}% to ${Math.round(event.remainingPct)}%, observed ${when}`,
      );
      details.push(`Reported rollover stayed ${formatClock(event.resetsAt, now)}`);
    }
  }
  if (account.provider === "codex" && (bank?.availableCount > 0 || recent.length > 0)) {
    details.push(
      "Codex resets can replace the weekly schedule. Use the current reported rollover.",
    );
    details.push("A full unused window may move when use starts.");
  }
  if (account.provider === "claude" && recent.length > 0)
    details.push(
      "Claude free resets preserve the usual weekly rollover. Use the current reported date above.",
    );
  return details;
}
