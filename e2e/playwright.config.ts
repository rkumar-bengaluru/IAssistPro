import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "*.e2e.ts",
  timeout: 15 * 60 * 1000,
  workers: 1,
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
});
