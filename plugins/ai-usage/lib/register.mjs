import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { childEnv, expandHome, resolveBinary, run } from "./exec.mjs";
import { editJsonSection, editZedContextServer } from "./harness-files.mjs";

const NAME = "ai-usage";

function pluginEnabled(claudeDir) {
  try {
    const settings = JSON.parse(readFileSync(join(claudeDir, "settings.json"), "utf8"));
    return Object.entries(settings.enabledPlugins ?? {}).some(([id, on]) => on && id.startsWith(`${NAME}@`));
  } catch {
    return false;
  }
}

function binary(name, override) {
  try {
    return resolveBinary(name, override);
  } catch {
    return null;
  }
}

/**
 * Every agent harness on this machine that can call the MCP server: each Claude Code profile, then any
 * installed Codex, agy, Gemini CLI, OpenCode, VS Code, Claude Desktop and Zed. CLIs with an `mcp add`
 * command are driven through it; the rest get a guarded config-file edit.
 */
export function registrationPlan(config, launcher, { remove = false, skipClaude = false, home = homedir() } = {}) {
  const plan = [];
  const cli = (target, bin, args, env = {}) => bin && plan.push({ kind: "cli", target, bin, args, env });
  const file = (target, apply) => plan.push({ kind: "file", target, apply });

  // Claude Code profiles with the ai-usage plugin enabled get the server from its .mcp.json; registering it
  // again at user scope would show every tool twice.
  const seenClaudeDirs = new Set();
  for (const account of config.accounts.filter((a) => a.provider === "claude" && !skipClaude)) {
    const dir = account.configDir ? resolve(expandHome(account.configDir)) : join(home, ".claude");
    if (seenClaudeDirs.has(dir)) continue;
    seenClaudeDirs.add(dir);
    const isDefault = dir === join(home, ".claude");
    const target = `Claude Code (${isDefault ? "~/.claude" : dir.replace(home, "~")})`;
    if (!remove && pluginEnabled(dir)) {
      const note = "provided by the ai-usage plugin";
      plan.push({ kind: "file", target, note, apply: () => ({ changed: false, note }) });
      continue;
    }
    cli(
      target,
      binary("claude", account.command),
      remove ? ["mcp", "remove", "--scope", "user", NAME] : ["mcp", "add", "--scope", "user", NAME, "--", launcher, "mcp"],
      { CLAUDE_CONFIG_DIR: isDefault ? null : dir },
    );
  }
  cli("Codex (CLI and app)", binary("codex"), remove ? ["mcp", "remove", NAME] : ["mcp", "add", NAME, "--", launcher, "mcp"]);
  cli("Antigravity (agy)", binary("agy"), remove ? ["mcp", "remove", NAME] : ["mcp", "add", NAME, launcher, "mcp"]);
  cli("Gemini CLI", binary("gemini"), remove ? ["mcp", "remove", "-s", "user", NAME] : ["mcp", "add", "-s", "user", NAME, launcher, "mcp"]);

  const code = binary("code");
  if (code) {
    if (remove) {
      file("VS Code", () => ({ changed: false, manual: `remove "${NAME}" from VS Code's user mcp.json (MCP: Open User Configuration)` }));
    } else {
      cli("VS Code", code, ["--add-mcp", JSON.stringify({ name: NAME, type: "stdio", command: launcher, args: ["mcp"] })]);
    }
  }

  const opencodeDir = join(home, ".config", "opencode");
  if (binary("opencode") || existsSync(opencodeDir)) {
    const jsonc = join(opencodeDir, "opencode.jsonc");
    const target = existsSync(jsonc) ? jsonc : join(opencodeDir, "opencode.json");
    file("OpenCode", () => editJsonSection(target, "mcp", NAME, { type: "local", command: [launcher, "mcp"], enabled: true }, { remove }));
  }

  const claudeDesktop = join(home, "Library", "Application Support", "Claude");
  if (existsSync(claudeDesktop)) {
    const target = join(claudeDesktop, "claude_desktop_config.json");
    file("Claude Desktop (restart it to load)", () => editJsonSection(target, "mcpServers", NAME, { command: launcher, args: ["mcp"] }, { remove }));
  }

  const zed = join(home, ".config", "zed", "settings.json");
  if (existsSync(zed)) {
    file("Zed", () => editZedContextServer(zed, NAME, { command: launcher, args: ["mcp"], env: {} }, { remove }));
  }
  return plan;
}

export async function registerMcp(config, { launcher, dryRun = false, remove = false, skipClaude = false } = {}) {
  for (const step of registrationPlan(config, launcher, { remove, skipClaude })) {
    if (step.kind === "file") {
      if (dryRun) {
        console.log(`${step.target}: ${step.note ?? "edit its config file"}`);
        continue;
      }
      try {
        const result = step.apply();
        const unchanged = result.note ?? (remove ? "not present" : "already registered");
        const status = result.manual ? `! ${step.target}: ${result.manual}` : `${result.changed ? "✓" : "•"} ${step.target}: ${result.changed ? (remove ? "removed" : "added") : unchanged}`;
        console.log(status);
      } catch (err) {
        console.log(`✗ ${step.target}: ${err.message}`);
      }
      continue;
    }
    const envPrefix = step.env.CLAUDE_CONFIG_DIR ? `CLAUDE_CONFIG_DIR=${step.env.CLAUDE_CONFIG_DIR} ` : step.env.CLAUDE_CONFIG_DIR === null ? "env -u CLAUDE_CONFIG_DIR " : "";
    const shown = `${envPrefix}${step.bin} ${step.args.map((a) => (/[\s"{]/.test(a) ? `'${a}'` : a)).join(" ")}`;
    if (dryRun) {
      console.log(`${step.target}:\n  ${shown}`);
      continue;
    }
    const env = childEnv(step.env, { strip: [/^CLAUDE_CODE_/, "CLAUDECODE"] });
    try {
      const { code, stdout, stderr } = await run(step.bin, step.args, { env, timeoutMs: 60_000 });
      const output = `${stdout}${stderr}`.trim().split("\n").filter((l) => !/not trusted|^\s*$/i.test(l));
      const already = output.some((l) => /already exists/i.test(l));
      console.log(`${code === 0 ? "✓" : already ? "•" : "✗"} ${step.target}: ${already ? "already registered" : output[0] || `exit ${code}`}`);
    } catch (err) {
      console.log(`✗ ${step.target}: ${err.message}`);
    }
  }
}
