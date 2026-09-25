import { spawn } from "node:child_process";
import { accessSync, constants } from "node:fs";
import { homedir } from "node:os";
import { delimiter, join } from "node:path";

export function expandHome(p) {
  if (typeof p !== "string") return p;
  return p === "~" ? homedir() : p.startsWith("~/") ? join(homedir(), p.slice(2)) : p;
}

const FALLBACK_DIRS = ["~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.volta/bin", "~/.bun/bin", "/usr/bin", "/bin"];

// GUI launches (the menu bar app, launchd) get a bare PATH, so fall back to the usual install locations.
export function resolveBinary(name, override) {
  if (override) return expandHome(override);
  const dirs = [...(process.env.PATH ?? "").split(delimiter), ...FALLBACK_DIRS.map(expandHome)];
  for (const dir of dirs) {
    if (!dir) continue;
    const candidate = join(dir, name);
    try {
      accessSync(candidate, constants.X_OK);
      return candidate;
    } catch {}
  }
  throw new Error(`${name} not found on PATH or in ${FALLBACK_DIRS.join(", ")}`);
}

export function childEnv(extra = {}, { strip = [] } = {}) {
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (strip.some((pattern) => (pattern instanceof RegExp ? pattern.test(key) : pattern === key))) delete env[key];
  }
  const fallbackPath = FALLBACK_DIRS.map(expandHome).join(delimiter);
  env.PATH = env.PATH ? `${env.PATH}${delimiter}${fallbackPath}` : fallbackPath;
  for (const [key, value] of Object.entries(extra)) {
    if (value === null) delete env[key];
    else env[key] = expandHome(String(value));
  }
  return env;
}

export function run(cmd, args, { env, timeoutMs = 60_000, maxBytes = 4 * 1024 * 1024, cwd } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { env, cwd: cwd ?? homedir(), stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`${cmd} timed out after ${Math.round(timeoutMs / 1000)}s`));
    }, timeoutMs);
    child.stdout.on("data", (d) => {
      stdout += d;
      if (stdout.length > maxBytes) child.kill("SIGKILL");
    });
    child.stderr.on("data", (d) => {
      if (stderr.length < 64 * 1024) stderr += d;
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}
