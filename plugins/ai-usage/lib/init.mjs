import { existsSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { resolveBinary } from "./exec.mjs";

const has = (name) => {
  try {
    resolveBinary(name);
    return true;
  } catch {
    return false;
  }
};

const titleCase = (s) => s.replace(/(^|[-_ ])(\w)/g, (_, sep, c) => (sep ? " " : "") + c.toUpperCase());

/**
 * Builds a starter config from what is installed on this machine: the default Claude profile, any extra
 * `~/.claude-<name>` profiles (the CLAUDE_CONFIG_DIR convention), Codex, and Antigravity's agy.
 * Only checks that files exist; never reads credentials.
 */
export function detectConfig({ home = homedir(), binaries = has, env = process.env } = {}) {
  const accounts = [];
  if (binaries("claude")) {
    const extra = readdirSync(home, { withFileTypes: true })
      .filter((d) => d.isDirectory() && /^\.claude-[\w-]+$/.test(d.name))
      .filter((d) => existsSync(join(home, d.name, ".claude.json")) || existsSync(join(home, d.name, "projects")))
      .map((d) => d.name.slice(".claude-".length));
    const multi = extra.length > 0;
    if (existsSync(join(home, ".claude")) || existsSync(join(home, ".claude.json"))) {
      accounts.push({ id: "claude", provider: "claude", label: multi ? "Claude · default" : "Claude", short: multi ? "CD" : "CL", route: "claude" });
    }
    for (const name of extra) {
      accounts.push({
        id: `claude-${name}`,
        provider: "claude",
        label: `Claude · ${titleCase(name).toLowerCase()}`,
        short: `C${name[0].toUpperCase()}`,
        configDir: `~/.claude-${name}`,
        route: `CLAUDE_CONFIG_DIR=~/.claude-${name} claude`,
      });
    }
  }
  if (binaries("codex")) accounts.push({ id: "codex", provider: "codex", label: "Codex · ChatGPT", short: "CX", route: "codex" });
  if (binaries("agy")) {
    accounts.push({
      id: "antigravity",
      provider: "antigravity",
      label: "Antigravity",
      short: "AG",
      route: "agy",
      routes: { gemini: "agy --model <gemini model>", "claude-gpt": "agy --model <claude or gpt-oss model>" },
    });
  }
  // Corporate TLS inspection: carry an extra CA bundle through to the child CLIs.
  const configEnv = env.NODE_EXTRA_CA_CERTS ? { NODE_EXTRA_CA_CERTS: env.NODE_EXTRA_CA_CERTS.replace(home, "~") } : {};
  return { maxAgeSeconds: 180, env: configEnv, accounts };
}
