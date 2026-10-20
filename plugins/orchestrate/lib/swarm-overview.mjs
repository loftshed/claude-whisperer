import { emitKeypressEvents } from "node:readline";
import { stripVTControlCharacters } from "node:util";

import { renderLine } from "../../monitor/lib/render.mjs";
import { fit } from "./stooges-view.mjs";

const clean = (text) =>
  [...stripVTControlCharacters(String(text ?? ""))]
    .map((character) => {
      const code = character.codePointAt(0);
      return code < 32 || (code >= 127 && code <= 159) ? " " : character;
    })
    .join("");

export function overviewLines({ rooms = [], errors = [] }, selected = 0, width = 100, height = 30) {
  const lines = [
    "Orkest swarms",
    "Enter open · f coordinator · v filter · c create · a add · d dispatch · r collect · q close",
    "",
  ];
  const available = Math.max(1, height - 5);
  const body = [];
  let selectedLine = 0;
  let previousGroup;
  for (const [index, room] of rooms.entries()) {
    const group = `${room.group || "Ungrouped"} · ${room.cwd}`;
    if (group !== previousGroup) body.push(clean(group));
    previousGroup = group;
    const counts = {};
    for (const member of room.agents)
      counts[member.status || "unknown"] = (counts[member.status || "unknown"] || 0) + 1;
    const summary = Object.entries(counts)
      .map(([status, count]) => `${count} ${status}`)
      .join(" · ");
    if (index === selected) selectedLine = body.length;
    body.push(
      `${index === selected ? ">" : " "} ${clean(room.title || room.id)} · ${summary || "No workers"}`,
    );
  }
  const start = Math.max(0, selectedLine - Math.floor(available / 2));
  lines.push(...body.slice(start, start + available));
  if (rooms.length === 0) lines.push("No swarms. Press c to create one.");
  for (const error of errors)
    lines.push(`Unreadable room ${clean(error.file)}: ${clean(error.error)}`);
  return lines.slice(0, Math.max(1, height - 2)).map((line) => fit(line, width));
}

export async function runOverview({
  load,
  quota,
  open,
  focus,
  filter,
  recover,
  form,
  input = process.stdin,
  output = process.stdout,
  interval = 3000,
}) {
  if (!input.isTTY || !output.isTTY) throw new Error("Overview requires a terminal; use --json");
  let data = { rooms: [], errors: [] };
  let selected = 0;
  let errorText = "";
  let quotaText = "";
  let isBusy = false;
  let isStopped = false;
  let timer;
  let terminalError;
  const { promise: completion, resolve: finish } = Promise.withResolvers();
  const isOldRaw = Boolean(input.isRaw);
  const paint = () => {
    if (isStopped) return;
    const lines = overviewLines(data, selected, output.columns || 100, output.rows || 30);
    lines.push(
      fit(clean(quotaText), output.columns || 100),
      fit(clean(errorText), output.columns || 100),
    );
    try {
      output.write(`\u{1B}[H${lines.map((line) => `\u{1B}[2K${line}`).join("\r\n")}\u{1B}[J`);
    } catch (error) {
      terminalError = error;
      close();
    }
  };
  const update = async () => {
    if (isBusy || isStopped) return;
    isBusy = true;
    try {
      const selectedId = data.rooms[selected]?.id;
      data = await load();
      data.rooms.sort((a, b) =>
        `${a.group}\0${a.cwd}\0${a.title || a.id}`.localeCompare(
          `${b.group}\0${b.cwd}\0${b.title || b.id}`,
        ),
      );
      selected = Math.max(
        0,
        data.rooms.findIndex((room) => room.id === selectedId),
      );
      if (quota) {
        const accounts = await quota();
        quotaText =
          accounts.length > 0 ? `Cached quota: ${renderLine(accounts)}` : "Quota cache unavailable";
      }
    } catch (error) {
      errorText = error.message;
    } finally {
      isBusy = false;
      paint();
    }
  };
  const close = () => {
    if (isStopped) return;
    isStopped = true;
    clearInterval(timer);
    input.off("keypress", keypress);
    input.off("end", close);
    input.off("close", close);
    output.off("resize", paint);
    process.off("SIGTERM", close);
    process.off("SIGINT", close);
    try {
      input.setRawMode(isOldRaw);
      input.pause();
    } catch (error) {
      terminalError ??= error;
    }
    try {
      output.write("\u{1B}[?25h\u{1B}[?1049l");
    } catch (error) {
      terminalError ??= error;
    }
    finish();
  };
  const keypress = async (text, key) => {
    if (text === "q" || (key?.ctrl && key.name === "c")) {
      close();
      return;
    }
    if (isBusy) return;
    if (key?.name === "down" || text === "n")
      selected = Math.min(data.rooms.length - 1, selected + 1);
    else if (key?.name === "up" || text === "p") selected = Math.max(0, selected - 1);
    else {
      const room = data.rooms[selected];
      isBusy = true;
      try {
        if (text === "c") await form("create");
        else if (room && key?.name === "return") await open(room.id);
        else if (room && text === "f") await focus(room.id, "conductor");
        else if (room && text === "v") await filter(room.id);
        else if (room && text === "a") await form("add", room.id);
        else if (room && text === "d") await form("dispatch", room.id);
        else if (room && text === "r") await recover(room.id);
        errorText = "";
      } catch (error) {
        errorText = error.message;
      } finally {
        isBusy = false;
      }
    }
    paint();
  };
  emitKeypressEvents(input);
  input.setRawMode(true);
  input.resume();
  input.on("keypress", keypress);
  input.on("end", close);
  input.on("close", close);
  output.on("resize", paint);
  process.on("SIGTERM", close);
  process.on("SIGINT", close);
  try {
    output.write("\u{1B}[?1049h\u{1B}[?25l");
    await update();
    if (!isStopped) timer = setInterval(update, interval);
    await completion;
  } finally {
    close();
  }
  if (terminalError) throw terminalError;
}
