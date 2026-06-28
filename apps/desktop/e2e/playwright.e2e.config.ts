import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

// Dedicated config for the comprehensive E2E + visual-regression + perf harness.
// Kept SEPARATE from apps/desktop/playwright.config.ts (which the existing
// `test:a11y` script and the a11y/walkthrough specs use) so the two suites never
// fight over testDir, reporters, or snapshot settings.
//
// Run from apps/desktop:
//   npx playwright test -c e2e/playwright.e2e.config.ts
//
// Network is mocked by default (KINORA_E2E_MOCK !== "0"), so this needs ONLY the
// Vite dev server — no backend, no Docker, no live Wan. The webServer block
// boots `npm run dev:web` and reuses an already-running :5173.

const ROOT = path.resolve(__dirname, "..");
const CI = !!process.env.CI;

export default defineConfig({
  testDir: path.join(__dirname, "specs"),
  // Visual snapshots live next to the visual specs. The {platform} segment keeps
  // macOS (local dev) and linux (CI container) baselines side by side, since
  // font hinting + AA differ across OSes — without it, locally-seeded Mac
  // baselines would always fail the Linux CI job.
  snapshotDir: path.join(__dirname, "visual", "__screenshots__"),
  snapshotPathTemplate: "{snapshotDir}/{testFilePath}/{platform}/{arg}-{projectName}{ext}",
  outputDir: path.join(ROOT, "test-results", "e2e"),

  fullyParallel: true,
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  workers: CI ? 2 : undefined,
  timeout: 60_000,
  expect: {
    timeout: 10_000,
    toHaveScreenshot: {
      // The renderer is animated + churny; tolerate sub-pixel AA + minor drift.
      maxDiffPixelRatio: 0.02,
      animations: "disabled",
      caret: "hide",
    },
  },

  reporter: CI
    ? [
        ["list"],
        ["html", { outputFolder: path.join(ROOT, "test-results", "e2e-report"), open: "never" }],
        ["json", { outputFile: path.join(ROOT, "test-results", "e2e-results.json") }],
        ["junit", { outputFile: path.join(ROOT, "test-results", "e2e-junit.xml") }],
        ["github"],
      ]
    : [["list"], ["html", { outputFolder: path.join(ROOT, "test-results", "e2e-report"), open: "never" }]],

  use: {
    baseURL: process.env.KINORA_E2E_BASE_URL ?? "http://localhost:5173",
    trace: "retain-on-failure",
    video: "retain-on-failure",
    screenshot: "only-on-failure",
    // Deterministic locale/timezone for stable text + the time-based greeting.
    locale: "en-US",
    timezoneId: "UTC",
    colorScheme: "dark",
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
      },
    },
  ],

  webServer: {
    command: "npm run dev:web",
    cwd: ROOT,
    url: "http://localhost:5173",
    reuseExistingServer: !CI,
    timeout: 120_000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
