#!/usr/bin/env node
// Install a separate passive recorder; never restart the existing Monitor app.
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

if (process.platform !== "darwin")
  throw new Error("Use observe.mjs with your scheduler on this platform");
const label = "local.monitor.observer";
const domain = `gui/${process.getuid()}`;
const plist = path.join(homedir(), "Library", "LaunchAgents", `${label}.plist`);
if (existsSync(plist) || spawnSync("launchctl", ["print", `${domain}/${label}`]).status === 0)
  throw new Error("An observer is already installed; its files and process were left unchanged");
const runtime = path.join(homedir(), ".local", "share", "monitor-observer");
const output = path.join(homedir(), ".local", "share", "monitor-observations");
const cache = process.env.MONITOR_CACHE_DIR ?? path.join(homedir(), ".cache", "monitor");
mkdirSync(runtime, { recursive: true, mode: 0o700 });
mkdirSync(output, { recursive: true, mode: 0o700 });
mkdirSync(path.dirname(plist), { recursive: true });
const script = path.join(runtime, "observe.mjs");
copyFileSync(new URL("../bin/observe.mjs", import.meta.url), script);
const escape = (s) => s.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const args = [process.execPath, script, "--cache-dir", cache, "--output-dir", output];
writeFileSync(
  plist,
  `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>${label}</string>
<key>ProgramArguments</key><array>${args.map((a) => `<string>${escape(a)}</string>`).join("")}</array>
<key>RunAtLoad</key><true/>
<key>StartInterval</key><integer>60</integer>
<key>ProcessType</key><string>Background</string>
<key>StandardErrorPath</key><string>${escape(path.join(output, "observer-errors.log"))}</string>
</dict></plist>\n`,
  { mode: 0o600 },
);
const result = spawnSync("launchctl", ["bootstrap", domain, plist], { encoding: "utf8" });
if (result.status !== 0)
  throw new Error(`Observer installation did not start: ${result.stderr.trim()}`);
console.log(`Observer started. Records: ${output}`);
