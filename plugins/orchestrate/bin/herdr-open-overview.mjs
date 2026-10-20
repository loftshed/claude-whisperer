#!/usr/bin/env node
import { herdrClient } from "../lib/herdr-client.mjs";
import { PLUGIN_ID } from "../lib/herdr-swarms.mjs";

try {
  if (!process.env.HERDR_WORKSPACE_ID) throw new Error("Invoke this action in a Herdr workspace");
  await herdrClient()("plugin.pane.open", {
    plugin_id: PLUGIN_ID,
    entrypoint: "overview",
    placement: "tab",
    focus: true,
    workspace_id: process.env.HERDR_WORKSPACE_ID,
  });
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
