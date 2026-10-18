import { readFile } from "node:fs/promises";
import path from "node:path";
import { emitKeypressEvents } from "node:readline";

import { CACHE_DIR, CONFIG_PATH } from "../../monitor/lib/store.mjs";
import { fit, StoogesView } from "../lib/stooges-view.mjs";

/** Follow monitor's shared snapshot without starting provider CLIs from a display. */
export async function cachedQuota() {
  let snapshot;
  try {
    snapshot = JSON.parse(await readFile(path.join(CACHE_DIR, "snapshot.json"), "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return [];
    throw error;
  }
  let config;
  try {
    config = JSON.parse(await readFile(CONFIG_PATH, "utf8"));
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const entries = snapshot.accounts || {};
  const configured = config?.accounts || Object.values(entries);
  return configured.map((account) => ({
    ...entries[account.id],
    id: account.id,
    label: account.label || account.id,
    short: account.short || account.id.slice(0, 2).toUpperCase(),
  }));
}

/** Own only terminal state; workers and room membership survive closing the display. */
export async function runTui(
  load,
  { input = process.stdin, output = process.stdout, quota = true } = {},
) {
  if (!input.isTTY || !output.isTTY)
    throw new Error("Interactive view needs a terminal; use --print or --json");
  const view = new StoogesView(await load(), { height: output.rows || 32 });
  let isBusy = false;
  let errorText = "";
  let isStopped = false;
  const refresh = async () => {
    if (isBusy || isStopped) return;
    isBusy = true;
    try {
      const room = await load();
      if (quota) {
        try {
          const accounts = await cachedQuota();
          if (accounts.length > 0) room.accounts = accounts;
        } catch (error) {
          room.quotaError = error.message;
        }
      }
      view.setRoom(room);
      errorText = "";
    } catch (error) {
      errorText = error.message;
    } finally {
      isBusy = false;
    }
  };
  const paint = () => {
    view.height = output.rows || 32;
    const lines = view.render(output.columns || 80);
    if (errorText) lines[lines.length - 1] = fit(errorText, output.columns || 80);
    // Erase each line and the remaining tail without flashing the whole screen.
    const frame = lines
      .slice(0, Math.max(1, view.height - 1))
      .map((line) => `\u{1B}[2K${line}`)
      .join("\r\n");
    output.write(`\u{1B}[H${frame}\u{1B}[J`);
  };
  const wasRaw = Boolean(input.isRaw);
  emitKeypressEvents(input);
  input.setRawMode(true);
  input.resume();
  output.write("\u{1B}[?1049h\u{1B}[?25l");
  await new Promise((resolve) => {
    const cleanup = () => {
      if (isStopped) return;
      isStopped = true;
      clearInterval(timer);
      clearInterval(refreshTimer);
      input.off("keypress", keypress);
      input.off("end", cleanup);
      process.off("SIGINT", cleanup);
      process.off("SIGTERM", cleanup);
      input.setRawMode(wasRaw);
      input.pause();
      output.write("\u{1B}[?25h\u{1B}[?1049l");
      resolve();
    };
    const keypress = (text, key) => {
      if (text === "q" || (key?.ctrl && key.name === "c")) cleanup();
      else {
        view.handleInput(key?.sequence || text || "");
        paint();
      }
    };
    const timer = setInterval(paint, 200);
    const refreshTimer = setInterval(refresh, 1000);
    input.on("keypress", keypress);
    input.on("end", cleanup);
    process.on("SIGINT", cleanup);
    process.on("SIGTERM", cleanup);
    paint();
    void refresh();
  });
}
