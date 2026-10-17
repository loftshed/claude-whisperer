import { readFileSync } from "node:fs";

export const VERSION = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
).version;

/** Version tag on `monitor json` output. Bump the suffix on any breaking change to its shape. */
export const SNAPSHOT_SCHEMA = "monitor.snapshot.v1";
