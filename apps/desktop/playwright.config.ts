import { defineConfig } from "@playwright/test";

/**
 * Electron e2e. Playwright launches the *built* app (out/main/index.js), so
 * `electron-vite build` must run first. On Linux CI this runs under xvfb; the
 * full login->library flow expects a backend + seeded book (see the CI job).
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: { trace: "on-first-retry" },
});
