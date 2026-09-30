import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*-test.js"],
    // CI keeps a JUnit report as a build artifact.
    outputFile: { junit: "junit.xml" },
    reporters: process.env.CI ? ["default", "junit"] : ["default"],
    // Some suites start real CLIs and hooks in temporary homes.
    testTimeout: 30_000,
  },
});
