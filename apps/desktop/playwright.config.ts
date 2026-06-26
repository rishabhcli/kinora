import { defineConfig, devices } from "@playwright/test";

// Drives the Vite-served renderer for the automated axe-core a11y scan and the
// keyboard-only walkthrough recording. No Electron / no backend required: the
// login screen renders standalone and the a11y harness mounts owned surfaces.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:5173",
    video: "on",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "npm run dev:web",
    url: "http://localhost:5173",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
