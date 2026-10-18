import { stripVTControlCharacters } from "node:util";

import { renderLine } from "../../monitor/lib/render.mjs";
import { coalesceMessages } from "./stooges-room.mjs";

const segments = new Intl.Segmenter(undefined, { granularity: "grapheme" });
const SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

// Provider output is data. Strip terminal controls before painting it into our screen.
export function plain(value) {
  return stripVTControlCharacters(String(value ?? "")).replaceAll(/[\p{Cc}\p{Cf}]/gu, " ");
}

function cellWidth(grapheme) {
  const cp = grapheme.codePointAt(0);
  if (/\p{Extended_Pictographic}|\p{Regional_Indicator}/u.test(grapheme)) return 2;
  if (
    cp >= 4352 &&
    (cp <= 4447 ||
      (cp >= 11_904 && cp <= 42_191) ||
      (cp >= 44_032 && cp <= 55_203) ||
      (cp >= 63_744 && cp <= 64_255) ||
      (cp >= 65_040 && cp <= 65_135) ||
      (cp >= 65_281 && cp <= 65_376) ||
      cp >= 131_072)
  )
    return 2;
  return /^\p{Mark}+$/u.test(grapheme) ? 0 : 1;
}

export function displayWidth(value) {
  return [...segments.segment(plain(value))].reduce(
    (sum, { segment }) => sum + cellWidth(segment),
    0,
  );
}

export function fit(value, width) {
  width = Math.max(0, Math.floor(width));
  let text = "";
  let used = 0;
  const graphemes = segments.segment(plain(value));
  for (const { segment } of graphemes) {
    const size = cellWidth(segment);
    if (used + size > width) break;
    text += segment;
    used += size;
  }
  return text + " ".repeat(width - used);
}

function box(title, lines, width) {
  if (width < 4) return [title, ...lines].map((line) => fit(line, width));
  return [
    `┌${fit(` ${title} `, width - 2)}┐`,
    ...lines.map((line) => `│${fit(line, width - 2)}│`),
    `└${"─".repeat(width - 2)}┘`,
  ];
}

function outputLines(agent) {
  const output = Array.isArray(agent?.output)
    ? agent.output
    : [agent?.output || "Waiting for output"];
  const lines = output.flatMap((line) => String(line).split("\n"));
  return lines.length > 0 ? lines : ["Waiting for output"];
}

function status(agent, now) {
  let symbol = "·";
  switch (agent.status) {
    case "running": {
      symbol = SPINNER[Math.floor(now / 200) % SPINNER.length];
      break;
    }
    case "blocked":
    case "failed": {
      symbol = "⚠";
      break;
    }
    case "completed": {
      symbol = "✓";
      break;
    }
    // No default
  }
  const elapsed = Number.isFinite(agent.startedAt)
    ? ` · ${Math.max(0, Math.floor((now - agent.startedAt) / 1000))}s`
    : "";
  return `${symbol} ${agent.id} · ${agent.status || "unknown"}${elapsed}`;
}

/** pi-tui compatible component: render(width): string[], handleInput(data), invalidate(). */
export class StoogesView {
  constructor(room, { now = Date.now, height = 32 } = {}) {
    this.room = room;
    this.now = now;
    this.height = height;
    this.focusId = room.agents[0]?.id;
    this.pinnedId = undefined;
    this.page = 0;
  }

  get pageSize() {
    return Math.min(9, Math.max(1, Math.floor((this.height - 12) / 2)));
  }

  setRoom(room) {
    this.room = room;
    if (room.agents.every((agent) => agent.id !== this.focusId)) this.focusId = room.agents[0]?.id;
    if (room.agents.every((agent) => agent.id !== this.pinnedId)) this.pinnedId = undefined;
    this.page = Math.min(this.page, Math.max(0, Math.ceil(room.agents.length / this.pageSize) - 1));
  }

  invalidate() {
    /* Projection reads the latest room and clock on each render. */
  }

  handleInput(key) {
    if (/^[1-9]$/u.test(key)) {
      if (Number(key) > this.pageSize) return;
      const agent = this.room.agents[this.page * this.pageSize + Number(key) - 1];
      if (agent) this.focusId = agent.id;
    } else
      switch (key) {
        case "p": {
          this.pinnedId = this.pinnedId === this.focusId ? undefined : this.focusId;
          break;
        }
        case "n": {
          this.page =
            (this.page + 1) % Math.max(1, Math.ceil(this.room.agents.length / this.pageSize));
          break;
        }
        case "\u{1B}[B":
        case "\u{1B}[A": {
          const agents = this.room.agents;
          const direction = key.endsWith("B") ? 1 : -1;
          const index =
            (agents.findIndex((agent) => agent.id === this.focusId) + direction + agents.length) %
            agents.length;
          this.focusId = agents[index]?.id;
          this.page = Math.floor((index || 0) / this.pageSize);

          break;
        }
        // No default
      }
  }

  render(width) {
    width = Math.max(1, Math.floor(width) || 80);
    if (this.height < 12) return box("ork", ["Resize terminal to at least 12 rows"], width);
    this.page = Math.min(
      this.page,
      Math.max(0, Math.ceil(this.room.agents.length / this.pageSize) - 1),
    );
    const now = this.now();
    const room = this.room;
    const quota = room.accounts?.length ? renderLine(room.accounts, now) : "Quota unavailable";
    const fetched = (room.accounts || [])
      .map((account) => account.fetchedAt)
      .filter(Number.isFinite);
    const age =
      fetched.length > 0
        ? ` · data ${Math.max(0, Math.floor((now - Math.min(...fetched)) / 1000))}s old`
        : "";
    const header = box(
      `ork · ${room.id}`,
      [
        `Conductor ${room.conductor?.status || "unknown"} · ${room.agents.length} agents`,
        room.quotaError ? `Quota unavailable: ${room.quotaError}` : `${quota}${age}`,
      ],
      width,
    );
    const body = [];
    if (room.agents.length > 0 && room.agents.length <= 3 && width >= 120 && this.height >= 24) {
      const columnWidth = Math.floor((width - 2) / 3);
      const columns = room.agents.map((agent, index) =>
        box(
          `[${index + 1}] ${status(agent, now)}`,
          [agent.question || `${agent.engine} · ${agent.model}`, ...outputLines(agent).slice(-6)],
          columnWidth,
        ),
      );
      const lines = Math.max(...columns.map((column) => column.length));
      for (let i = 0; i < lines; i++)
        body.push(columns.map((column) => fit(column[i] || "", columnWidth)).join(" "));
    } else {
      const visible = room.agents.slice(
        this.page * this.pageSize,
        this.page * this.pageSize + this.pageSize,
      );
      for (const [index, agent] of visible.entries()) {
        const marker = agent.id === this.focusId ? ">" : " ";
        body.push(`${marker}[${index + 1}] ${status(agent, now)}`);
        body.push(`    ${agent.question || `${agent.engine} · ${agent.model}`}`);
      }
      if (room.agents.length === 0)
        body.push("No agents. Use ork stooges spawn --attach-room <id> --team <team>");
    }
    const agent = room.agents.find((item) => item.id === (this.pinnedId || this.focusId));
    const available = Math.max(1, this.height - header.length - body.length - 7);
    const detail = box(
      `${this.pinnedId ? "Pinned" : "Focus"} · ${agent?.id || "none"}`,
      outputLines(agent).slice(-Math.min(8, available)),
      width,
    );
    const messages = coalesceMessages(room.messages)
      .slice(-2)
      .map(
        (message) =>
          `${message.from}: ${message.text}${message.count > 1 ? ` (×${message.count})` : ""}`,
      );
    return [
      ...header,
      ...body,
      ...detail,
      ...messages,
      `1–9 focus · p pin · n page ${this.page + 1}/${Math.max(1, Math.ceil(room.agents.length / this.pageSize))} · q quit`,
    ].map((line) => fit(line, width));
  }
}
