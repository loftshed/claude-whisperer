import { readFile } from "node:fs/promises";
import path from "node:path";
import { emitKeypressEvents } from "node:readline";

import { shouldUseColor } from "../../monitor/lib/render.mjs";
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
  const view = new StoogesView(await load(), {
    height: output.rows || 32,
    color: shouldUseColor(output),
  });
  let isBusy = false;
  let errorText = "";
  let isStopped = false;
  let isRawModeChanged = false;
  let isInputResumed = false;
  let isAlternateScreen = false;
  let isCursorHidden = false;
  let lastFrame = "";
  let terminalError;
  let timer;
  let refreshTimer;
  let resolveExit;
  const wasRaw = Boolean(input.isRaw);
  const width = () => output.columns || 80;
  const height = () => output.rows || 32;
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
    paint();
  };
  const paint = () => {
    if (isStopped) return;
    try {
      view.height = height();
      const lines = view.render(width());
      if (errorText && lines.length > 0) lines[lines.length - 1] = fit(errorText, width());
      // Erase each line and the remaining tail without flashing a cleared screen.
      const frame = lines
        .slice(0, Math.max(1, view.height))
        .map((line) => `\u{1B}[2K${line}`)
        .join("\r\n");
      const nextFrame = `\u{1B}[H${frame}\u{1B}[J`;
      if (nextFrame !== lastFrame) {
        output.write(nextFrame);
        lastFrame = nextFrame;
      }
    } catch (error) {
      terminalError = error;
      cleanup();
    }
  };
  const cleanup = () => {
    if (isStopped) return;
    isStopped = true;
    clearInterval(timer);
    clearInterval(refreshTimer);
    input.off("keypress", keypress);
    input.off("end", cleanup);
    input.off("close", cleanup);
    process.off("SIGINT", cleanup);
    process.off("SIGTERM", cleanup);
    output.off?.("resize", onResize);
    try {
      if (isRawModeChanged) input.setRawMode(wasRaw);
    } catch (error) {
      // Still restore the visible terminal even if the stream has already closed.
      terminalError ??= error;
    }
    if (isInputResumed) {
      try {
        input.pause();
      } catch (error) {
        terminalError ??= error;
      }
    }
    if (isAlternateScreen || isCursorHidden) {
      try {
        output.write(
          `${isCursorHidden ? "\u{1B}[?25h" : ""}${isAlternateScreen ? "\u{1B}[?1049l" : ""}`,
        );
      } catch (error) {
        terminalError ??= error;
      }
    }
    lastFrame = "";
    resolveExit?.();
  };
  const keypress = (text, key) => {
    const value = key?.sequence || text || "";
    if (text === "q" || (key?.ctrl && key.name === "c")) cleanup();
    else {
      view.handleInput(value);
      lastFrame = "";
      paint();
    }
  };
  const onResize = () => {
    lastFrame = "";
    paint();
  };
  try {
    emitKeypressEvents(input);
    input.setRawMode(true);
    isRawModeChanged = true;
    input.resume();
    isInputResumed = true;
    output.write("\u{1B}[?1049h\u{1B}[?25l");
    isAlternateScreen = true;
    isCursorHidden = true;
    await new Promise((resolve) => {
      resolveExit = resolve;
      timer = setInterval(paint, 200);
      refreshTimer = setInterval(refresh, 1000);
      input.on("keypress", keypress);
      input.on("end", cleanup);
      input.on("close", cleanup);
      process.on("SIGINT", cleanup);
      process.on("SIGTERM", cleanup);
      output.on?.("resize", onResize);
      paint();
      void refresh();
    });
  } finally {
    cleanup();
  }
  if (terminalError) throw terminalError;
}
