import { stripVTControlCharacters } from "node:util";

import { renderLine } from "../../monitor/lib/render.mjs";
import { coalesceMessages } from "./stooges-room.mjs";

const segments = new Intl.Segmenter(undefined, { granularity: "grapheme" });
const SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const ESC = String.fromCodePoint(27);
const ANSI_SGR_PATTERN = new RegExp(String.raw`${ESC}\[[0-9;]*m|[^${ESC}]+`, "gu");
const COLORS = {
  cyan: "36",
  green: "32",
  yellow: "33",
  red: "31",
  dim: "2",
  bold: "1",
  inverse: "7",
};

const GLYPHS = {
  ascii: {
    topLeft: "+",
    topRight: "+",
    bottomLeft: "+",
    bottomRight: "+",
    horizontal: "-",
    vertical: "|",
    separator: "-",
    filled: "#",
    empty: ".",
    running: ">",
    blocked: "!",
    completed: "+",
    idle: "-",
  },
  unicode: {
    topLeft: "┌",
    topRight: "┐",
    bottomLeft: "└",
    bottomRight: "┘",
    horizontal: "─",
    vertical: "│",
    separator: "┄",
    filled: "█",
    empty: "░",
    running: SPINNER[0],
    blocked: "⚠",
    completed: "✓",
    idle: "·",
  },
  nerd: {
    topLeft: "╭",
    topRight: "╮",
    bottomLeft: "╰",
    bottomRight: "╯",
    horizontal: "─",
    vertical: "│",
    separator: "┄",
    filled: "█",
    empty: "░",
    running: "󰔛",
    blocked: "󰀪",
    completed: "󰄬",
    idle: "󰘓",
  },
};

/** Use explicit Nerd Font selection; otherwise stay with Unicode or an ASCII-safe locale. */
export function glyphTier(env = process.env) {
  const requested = env.ORK_GLYPHS?.toLowerCase();
  if (Object.hasOwn(GLYPHS, requested)) return requested;
  const locale = String(env.LC_ALL || env.LC_CTYPE || env.LANG || "").toLowerCase();
  if (env.TERM === "dumb" || !/(?:utf-?8|utf8)/u.test(locale)) return "ascii";
  return "unicode";
}

export function glyphSet(tier = glyphTier()) {
  return GLYPHS[Object.hasOwn(GLYPHS, tier) ? tier : "unicode"];
}

// Provider output is data. Strip terminal controls before adding our own ANSI styling.
export function plain(value) {
  return stripVTControlCharacters(String(value ?? "")).replaceAll(
    /[\p{Cc}\p{Cf}]/gu,
    (character) => (character === "\u{200C}" || character === "\u{200D}" ? character : " "),
  );
}

function cellWidth(grapheme) {
  if (
    /[\u{1F1E6}-\u{1F1FF}]/u.test(grapheme) ||
    /\p{Extended_Pictographic}/u.test(grapheme) ||
    /[\u{FE0F}\u{20E3}]/u.test(grapheme)
  )
    return 2;
  const cp = grapheme.codePointAt(0);
  if (
    cp >= 4352 &&
    (cp <= 4447 ||
      (cp >= 11_904 && cp <= 12_663) ||
      (cp >= 12_864 && cp <= 12_895) ||
      (cp >= 13_312 && cp <= 19_823) ||
      (cp >= 19_968 && cp <= 20_367) ||
      (cp >= 20_880 && cp <= 21_471) ||
      (cp >= 21_472 && cp <= 23_199) ||
      (cp >= 23_552 && cp <= 25_919) ||
      (cp >= 25_920 && cp <= 26_047) ||
      (cp >= 26_112 && cp <= 26_143) ||
      (cp >= 26_144 && cp <= 26_527) ||
      (cp >= 26_528 && cp <= 26_559) ||
      (cp >= 26_560 && cp <= 27_103) ||
      (cp >= 27_136 && cp <= 27_167) ||
      (cp >= 27_200 && cp <= 27_231) ||
      (cp >= 27_264 && cp <= 27_295) ||
      (cp >= 27_328 && cp <= 27_423) ||
      (cp >= 27_456 && cp <= 27_487) ||
      (cp >= 27_520 && cp <= 27_583) ||
      (cp >= 27_616 && cp <= 28_671) ||
      (cp >= 28_672 && cp <= 30_719) ||
      (cp >= 30_720 && cp <= 30_783) ||
      (cp >= 30_784 && cp <= 31_967) ||
      (cp >= 31_968 && cp <= 32_767) ||
      (cp >= 32_768 && cp <= 33_583) ||
      (cp >= 33_584 && cp <= 34_047) ||
      (cp >= 34_048 && cp <= 34_815) ||
      (cp >= 44_032 && cp <= 55_203) ||
      (cp >= 63_744 && cp <= 64_255) ||
      (cp >= 65_040 && cp <= 65_135) ||
      (cp >= 65_281 && cp <= 65_376) ||
      (cp >= 131_072 && cp <= 262_141))
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

export function wrapText(value, width) {
  width = Math.max(1, Math.floor(width));
  const output = [];
  let line = "";
  const tokens = plain(value).match(/\s*\S+/gu) || [];
  for (const token of tokens) {
    const candidate = line + token;
    if (line && displayWidth(candidate) > width) {
      output.push(line.trimEnd());
      line = "";
    }
    let word = line ? token : token.trimStart();
    const graphemes = segments.segment(word);
    for (const { segment } of graphemes) {
      if (cellWidth(segment) > width) {
        if (line) output.push(line);
        output.push("?");
        line = "";
        continue;
      }
      if (line && displayWidth(line + segment) > width) {
        output.push(line);
        line = "";
      }
      line += segment;
    }
  }
  if (line || output.length === 0) output.push(line);
  return output;
}

export function progressBar(value, width, tier = glyphTier()) {
  const glyphs = glyphSet(tier);
  width = Math.max(0, Math.floor(width));
  const ratio = Math.max(0, Math.min(1, Number(value) || 0));
  const eighths = Math.round(ratio * width * 8);
  const full = Math.floor(eighths / 8);
  if (tier === "ascii") {
    const cells = Math.round(eighths / 8);
    return glyphs.filled.repeat(cells) + glyphs.empty.repeat(width - cells);
  }
  const partial = eighths % 8;
  const fraction = ["", "▏", "▎", "▍", "▌", "▋", "▊", "▉"][partial];
  const empty = Math.max(0, width - full - Number(Boolean(fraction)));
  return glyphs.filled.repeat(full) + fraction + glyphs.empty.repeat(empty);
}

function ansi(value, tone, color) {
  const text = plain(value);
  const codes = String(tone)
    .split(/\s+/u)
    .map((name) => COLORS[name])
    .filter(Boolean);
  if (!color || codes.length === 0) return text;
  return `\u{1B}[${codes.join(";")}m${text}\u{1B}[0m`;
}

function fitStyled(value, width) {
  width = Math.max(0, Math.floor(width));
  const source = String(value ?? "");
  const tokens = source.match(ANSI_SGR_PATTERN) || [];
  let output = "";
  let used = 0;
  let isStyled = false;
  for (const token of tokens) {
    if (token.startsWith(`${ESC}[`)) {
      output += token;
      isStyled = !token.startsWith(`${ESC}[0m`);
      continue;
    }
    const graphemes = segments.segment(plain(token));
    for (const { segment } of graphemes) {
      const size = cellWidth(segment);
      if (!(used + size <= width)) {
        continue;
      }

      output += segment;
      used += size;
    }
  }
  if (used < width) output += " ".repeat(width - used);
  if (isStyled) output += "\u{1B}[0m";
  return output;
}

function frame(title, lines, width, { glyphs = glyphSet(), color = false, right = "" } = {}) {
  width = Math.max(1, Math.floor(width));
  if (width < 4) return [title, ...lines].map((line) => fit(line, width));
  const inside = width - 2;
  const cleanTitle = ` ${plain(title)} `;
  const cleanRight = right ? ` ${plain(right)} ` : "";
  const availableTitle = Math.max(0, inside - displayWidth(cleanRight));
  const heading = fit(cleanTitle, availableTitle);
  const tail = fit(cleanRight, inside - displayWidth(heading));
  const topContent = heading + tail;
  const topRemaining = Math.max(0, inside - displayWidth(topContent));
  const top = ansi(
    glyphs.topLeft + topContent + glyphs.horizontal.repeat(topRemaining) + glyphs.topRight,
    "cyan",
    color,
  );
  return [
    top,
    ...lines.map(
      (line) =>
        `${ansi(glyphs.vertical, "cyan", color)}${fitStyled(line, inside)}${ansi(glyphs.vertical, "cyan", color)}`,
    ),
    ansi(
      `${glyphs.bottomLeft}${glyphs.horizontal.repeat(inside)}${glyphs.bottomRight}`,
      "cyan",
      color,
    ),
  ];
}

/** Shared framed panel for interactive and non-interactive terminal output. */
export function renderPanel(title, lines, width, options = {}) {
  const margin = Math.min(1, Math.floor((Math.max(1, width) - 1) / 2));
  const panelWidth = Math.max(1, Math.floor(width) - margin * 2);
  return frame(title, lines.map(plain), panelWidth, options).map(
    (line) => `${" ".repeat(margin)}${line}`,
  );
}

/** Fit weighted cells into a fixed width, keeping separators inside the available space. */
export function renderTable(
  headers,
  rows,
  width,
  { weights = [], tier = glyphTier(), rowDetails = [], rowStyle } = {},
) {
  width = Math.max(headers.length, Math.floor(width));
  const separator = ` ${glyphSet(tier).vertical} `;
  const separatorWidth = displayWidth(separator) * Math.max(0, headers.length - 1);
  const cellWidthTotal = Math.max(headers.length, width - separatorWidth);
  const ratios = headers.map((_, index) => Math.max(0.1, Number(weights[index]) || 1));
  const ratioTotal = ratios.reduce((sum, ratio) => sum + ratio, 0);
  const widths = ratios.map((ratio) =>
    Math.max(1, Math.floor((cellWidthTotal * ratio) / ratioTotal)),
  );
  let remaining = cellWidthTotal - widths.reduce((sum, cell) => sum + cell, 0);
  for (let index = 0; remaining > 0; index = (index + 1) % widths.length, remaining--)
    widths[index]++;
  while (widths.reduce((sum, cell) => sum + cell, 0) + separatorWidth > width) {
    const index = widths.indexOf(Math.max(...widths));
    if (widths[index] <= 1) break;
    widths[index]--;
  }
  const renderRow = (row) => row.map((cell, index) => fit(cell, widths[index])).join(separator);
  const renderedRows = rows.flatMap((row, rowIndex) => {
    const cells = row.map((cell, index) => wrapText(cell, widths[index]));
    const height = Math.max(1, ...cells.map((lines) => lines.length));
    const lines = Array.from({ length: height }, (_, line) =>
      cells.map((cellLines, index) => fit(cellLines[line] || "", widths[index])).join(separator),
    );
    const detail = rowDetails[rowIndex];
    if (detail) lines.push(...wrapText(detail, width));
    return lines.map((line) => (rowStyle ? rowStyle(line, rowIndex) : line));
  });
  return [
    renderRow(headers),
    widths.map((cell) => glyphSet(tier).separator.repeat(cell)).join(separator),
    ...renderedRows,
  ];
}

function toneForStatus(status) {
  if (status === "completed") return "green";
  if (status === "running") return "cyan";
  if (status === "blocked" || status === "failed") return "red";
  if (status === "queued") return "yellow";
  return "dim";
}

function statusGlyph(agent, now, tier) {
  const glyphs = glyphSet(tier);
  if (agent.status === "running" && tier === "unicode")
    return SPINNER[Math.floor(now / 200) % SPINNER.length];
  if (agent.status === "running") return glyphs.running;
  if (agent.status === "blocked" || agent.status === "failed") return glyphs.blocked;
  if (agent.status === "completed") return glyphs.completed;
  return glyphs.idle;
}

function statusLabel(agent, now, tier) {
  const elapsed = Number.isFinite(agent.startedAt)
    ? ` ${Math.max(0, Math.floor((now - agent.startedAt) / 1000))}s`
    : "";
  return `${statusGlyph(agent, now, tier)} ${agent.status || "unknown"}${elapsed}`;
}

function outputLines(agent) {
  const output = Array.isArray(agent?.output)
    ? agent.output
    : [agent?.output || "Waiting for output"];
  const lines = output.flatMap((line) =>
    String(line ?? "")
      .split(/\r?\n/u)
      .map(plain),
  );
  return lines.length > 0 ? lines : ["Waiting for output"];
}

function quotaRows(accounts, width, tier, color) {
  const rows = [];
  const accountList = accounts || [];
  for (const account of accountList) {
    const windows = Array.isArray(account.windows) ? account.windows : [];
    const short = account.short || account.id || "?";
    if (windows.length === 0) {
      rows.push(plain(`${short} ${renderLine([account]) || "quota unavailable"}`));
      continue;
    }
    const displayedWindows = windows.slice(0, 3);
    for (const window of displayedWindows) {
      const pct = Number.isFinite(window.remainingPct)
        ? Math.max(0, Math.min(100, window.remainingPct))
        : undefined;
      const label = plain(`${short} ${window.label || window.id || "quota"}`);
      const suffixWidth = pct === undefined ? 12 : displayWidth(`${Math.round(pct)}% left`) + 1;
      const barWidth = Math.max(3, Math.min(18, width - displayWidth(label) - suffixWidth - 1));
      rows.push(
        pct === undefined
          ? `${label} unavailable`
          : `${label} ${ansi(progressBar(pct / 100, barWidth, tier), pct <= 10 ? "red" : "green", color)} ${Math.round(pct)}% left`,
      );
    }
  }
  return rows.length > 0 ? rows : ["Quota unavailable"];
}

function statusCounts(agents) {
  const counts = { completed: 0, running: 0, blocked: 0, failed: 0, queued: 0, other: 0 };
  for (const agent of agents) counts[counts[agent.status] === undefined ? "other" : agent.status]++;
  return counts;
}

function rangeLabel(start, end, total) {
  return total === 0 ? "0 agents" : `${start + 1}–${end} of ${total} agents`;
}

/** pi-tui compatible component: render(width): string[], handleInput(data), invalidate(). */
export class StoogesView {
  constructor(room, { now = Date.now, height = 32, color = false, tier = glyphTier() } = {}) {
    this.room = room;
    this.now = now;
    this.height = height;
    this.color = color;
    this.tier = tier;
    this.focusId = room.agents[0]?.id;
    this.pinnedId = undefined;
    this.page = 0;
    this.overlay = undefined;
    this.logOffset = 0;
  }

  pageSizeFor() {
    const questions = Math.min(4, this.room.agents.filter((agent) => agent.question).length);
    const bodyRows = Math.max(1, this.height - 18 - questions);
    return Math.min(9, bodyRows);
  }

  get pageSize() {
    return this.pageSizeFor();
  }

  get needsAnimation() {
    return this.room.agents.some((agent) => agent.status === "running");
  }

  setRoom(room) {
    this.room = room;
    if (room.agents.every((agent) => agent.id !== this.focusId)) this.focusId = room.agents[0]?.id;
    if (room.agents.every((agent) => agent.id !== this.pinnedId)) this.pinnedId = undefined;
    this.page = Math.min(this.page, Math.max(0, Math.ceil(room.agents.length / this.pageSize) - 1));
  }

  marginLines(lines, width) {
    const margin = Math.min(1, Math.floor((Math.max(1, width) - 1) / 2));
    const contentWidth = Math.max(1, width - margin * 2);
    return lines
      .slice(0, Math.max(0, this.height))
      .map((line) => `${" ".repeat(margin)}${fitStyled(line, contentWidth)}`);
  }

  invalidate() {
    /* Projection reads the latest room and clock on each render. */
  }

  handleInput(key) {
    if (this.overlay) {
      switch (key) {
        case "\u{1B}":
        case "\u{1B}[O": {
          this.overlay = undefined;
          break;
        }
        case "?":
        case "g": {
          this.overlay = key === "?" ? "help" : "logs";
          break;
        }
        case "\u{1B}[B":
        case "n": {
          this.logOffset++;
          break;
        }
        case "\u{1B}[A":
        case "p": {
          {
            this.logOffset = Math.max(0, this.logOffset - 1);
            // No default
          }
          break;
        }
      }
      return;
    }
    if (key === "?") {
      this.overlay = "help";
      return;
    }
    if (key === "g") {
      this.overlay = "logs";
      this.logOffset = 0;
      return;
    }
    if (/^[1-9]$/u.test(key)) {
      const selected = Number(key) - 1;
      const agent = this.room.agents[this.page * this.pageSize + selected];
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
          if (this.room.agents.length === 0) break;
          const agents = this.room.agents;
          const direction = key.endsWith("B") ? 1 : -1;
          const current = agents.findIndex((agent) => agent.id === this.focusId);
          const index = (current + direction + agents.length) % agents.length;
          this.focusId = agents[index].id;
          this.page = Math.floor(index / this.pageSize);
          break;
        }
        // No default
      }
  }

  panel(title, body, width, right = "") {
    return frame(title, body, width, { glyphs: glyphSet(this.tier), color: this.color, right });
  }

  overlayLines(width) {
    const title = this.overlay === "help" ? "Key reference" : "Room log";
    const messages = coalesceMessages(this.room.messages).map(
      (message) =>
        `${plain(message.from)}: ${plain(message.text)}${message.count > 1 ? ` (×${message.count})` : ""}`,
    );
    let rows;
    if (this.overlay === "help") {
      rows = [
        "↑/↓ or 1–9  Select agent",
        "n           Next page",
        "p           Pin or unpin focused output",
        "g           Open coalesced room log",
        "? or Esc    Close this reference",
        "q or Ctrl-C Quit the display; workers continue",
        "Glyphs      Set ORK_GLYPHS=ascii|unicode|nerd",
        "Nerd tier   Requires a Nerd Font; selected explicitly",
      ];
    } else {
      rows = messages.length > 0 ? messages : ["No room messages yet"];
    }
    const interior = Math.max(1, width - 6);
    const wrapped = rows.flatMap((line) => wrapText(line, interior));
    if (this.overlay === "logs") {
      const count = Math.max(1, this.height - 7);
      const start = Math.max(0, Math.min(this.logOffset, wrapped.length - count));
      const visible = wrapped.slice(start, start + count);
      return this.panel(title, visible, width, "Esc close");
    }
    return this.panel(title, wrapped.slice(0, Math.max(1, this.height - 6)), width, "Esc close");
  }

  quotaLines(width) {
    if (this.room.quotaError) return [`Quota unavailable: ${plain(this.room.quotaError)}`];
    const accounts = this.room.accounts || [];
    return quotaRows(accounts, Math.max(10, width - 4), this.tier, this.color);
  }

  agentRows(width, now, pageSize) {
    const room = this.room;
    const start = this.page * pageSize;
    const visible = room.agents.slice(start, start + pageSize);
    const focused = this.focusId;
    const isCompact = width < 48;
    const isMedium = width < 64;
    const isStandard = width < 80;
    let headers;
    let weights;
    let rows;
    let selectedIndex;
    if (isCompact) {
      headers = ["#", "Agent / status"];
      weights = [0.15, 0.85];
      rows = visible.map((agent, index) => [
        String(index + 1),
        `${agent.id} · ${agent.status || "unknown"}`,
      ]);
    } else if (isMedium) {
      headers = ["#", "Agent", "Status"];
      weights = [0.1, 0.52, 0.38];
      rows = visible.map((agent, index) => [
        String(index + 1),
        agent.id,
        statusLabel(agent, now, this.tier),
      ]);
    } else if (isStandard) {
      headers = ["#", "Agent", "Model", "Status"];
      weights = [0.07, 0.27, 0.36, 0.3];
      rows = visible.map((agent, index) => [
        String(index + 1),
        agent.id,
        `${agent.engine} · ${agent.model}`,
        statusLabel(agent, now, this.tier),
      ]);
    } else {
      headers = ["#", "Agent", "Engine / model", "Status", "Elapsed"];
      weights = [0.05, 0.2, 0.35, 0.28, 0.12];
      rows = visible.map((agent, index) => [
        String(index + 1),
        agent.id,
        `${agent.engine} · ${agent.model}`,
        statusLabel(agent, now, this.tier),
        Number.isFinite(agent.startedAt)
          ? `${Math.max(0, Math.floor((now - agent.startedAt) / 1000))}s`
          : "—",
      ]);
    }
    const tableWidth = Math.max(headers.length, width - 2);
    const table = renderTable(headers, rows, tableWidth, {
      weights,
      tier: this.tier,
      rowDetails: visible.map((agent) => (agent.question ? `? ${plain(agent.question)}` : "")),
      rowStyle: (line, index) => {
        const tone = toneForStatus(visible[index]?.status);
        const isSelected = index === visible.findIndex((agent) => agent.id === focused);
        return ansi(line, `${tone}${isSelected ? " inverse" : ""}`, this.color);
      },
    });
    const headerRows = table.slice(0, 2);
    const selectedAgentIndex = visible.findIndex((agent) => agent.id === focused);
    selectedIndex = selectedAgentIndex;
    const panelRows = [
      ...headerRows.map((line) => ansi(line, "bold", this.color)),
      ...table.slice(2),
    ];
    if (room.agents.length === 0) panelRows.push("No agents queued for this room");
    const end = Math.min(room.agents.length, start + pageSize);
    const range = rangeLabel(start, end, room.agents.length);
    const rangeFooter =
      room.agents.length > 0 ? `Rows ${start + 1}–${end}/${room.agents.length}` : "Rows 0/0";
    return {
      lines: panelRows,
      range,
      rangeFooter,
      visible,
      selectedIndex,
      columnCount: headers.length,
    };
  }

  render(width) {
    width = Math.max(1, Math.floor(width) || 80);
    if (this.overlay) return this.marginLines(this.overlayLines(Math.max(1, width - 2)), width);
    if (this.height < 8)
      return this.marginLines(
        this.panel("ork", ["Resize terminal to at least 8 rows"], width),
        width,
      );

    const now = this.now();
    const room = this.room;
    const pageSize = this.pageSizeFor();
    const pageCount = Math.max(1, Math.ceil(room.agents.length / pageSize));
    this.page = Math.min(this.page, pageCount - 1);
    const counts = statusCounts(room.agents);
    const done = counts.completed;
    const total = room.agents.length;
    const title = `ork · ${room.id}`;
    const headerWidth = Math.max(1, width - 2);
    const barWidth = Math.max(4, Math.min(18, headerWidth - 40));
    const header = this.panel(
      title,
      [
        `Conductor ${plain(room.conductor?.status || "unknown")} · ${total} agents`,
        `${done}/${total} complete ${progressBar(total ? done / total : 0, barWidth, this.tier)}`,
      ],
      headerWidth,
      "[?] help",
    );
    const quotaLines = this.quotaLines(headerWidth);
    let quotaLimit = 4;
    if (this.height < 32) quotaLimit = 2;
    if (this.height < 24) quotaLimit = 1;
    const quota = this.panel("Quota", quotaLines.slice(0, quotaLimit), headerWidth);
    const agentTable = this.agentRows(headerWidth, now, pageSize);
    const pageLabel = `Showing ${agentTable.range} · page ${this.page + 1}/${pageCount}`;
    const agents = this.panel("Agents", agentTable.lines, headerWidth, pageLabel);
    const agent = room.agents.find((item) => item.id === (this.pinnedId || this.focusId));
    const detailLines = Math.max(
      1,
      Math.min(this.height - header.length - quota.length - agents.length - 3, 4),
    );
    const outputCount = Math.max(0, detailLines - 1 - Number(Boolean(agent?.question)));
    const detailBody = agent
      ? [
          plain(`${agent.engine} · ${agent.model} · ${statusLabel(agent, now, this.tier)}`),
          ...(agent.question ? [`Question: ${plain(agent.question)}`] : []),
          ...(outputCount > 0 ? outputLines(agent).slice(-outputCount) : []),
        ]
      : ["Select an agent to view output"];
    const detail = this.panel(
      `${this.pinnedId ? "Pinned" : "Focus"} · ${agent?.id || "none"}`,
      detailBody.slice(0, detailLines),
      headerWidth,
      "[g] logs",
    );
    const messages = coalesceMessages(room.messages)
      .slice(-1)
      .map(
        (message) =>
          `${plain(message.from)}: ${plain(message.text)}${message.count > 1 ? ` (×${message.count})` : ""}`,
      );
    const footer =
      width < 48
        ? `${agentTable.rangeFooter} · n page · q quit`
        : `${agentTable.rangeFooter} · ↑↓ · # select · n · p · ? · g · q quit`;
    const all = [...header, ...quota, ...agents, ...detail, footer];
    if (this.height >= 28) all.push(...messages);
    return this.marginLines(all, width);
  }
}
