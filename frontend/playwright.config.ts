import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright e2e config for Kinora (Phase 13).
 *
 * The suite drives the REAL frontend against a REAL running backend + a
 * deterministic, pre-ingested book (seeded by `backend/scripts/seed_e2e.py`),
 * so it is fast (seconds) and needs no Wan/Qwen/CosyVoice spend.
 *
 * Stack the config expects to be up before the run (the CI `e2e` job and the
 * local Makefile/commands below bring this up):
 *   1. Postgres(+pgvector) + Redis + MinIO (throwaway).
 *   2. `alembic upgrade head`.
 *   3. The API gateway: `uvicorn app.main:app` on $KINORA_API_TARGET (:8000).
 *   4. `python backend/scripts/seed_e2e.py` (creates the e2e user + book).
 * Playwright then starts the frontend dev server (which proxies `/api` to the
 * gateway) via the `webServer` block below and runs the specs.
 *
 * Env overrides (used for local runs on non-default ports; CI uses defaults):
 *   KINORA_WEB_PORT      frontend port               (default 5173)
 *   KINORA_E2E_BASE_URL  full base URL               (default http://localhost:$PORT)
 *   KINORA_API_TARGET    backend the dev proxy hits  (default http://localhost:8000)
 *   KINORA_REDIS_HOST / KINORA_REDIS_PORT  for the event publisher (default 127.0.0.1:6379)
 */

const WEB_PORT = process.env.KINORA_WEB_PORT ?? "5173";
const BASE_URL = process.env.KINORA_E2E_BASE_URL ?? `http://localhost:${WEB_PORT}`;
const API_TARGET = process.env.KINORA_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  testDir: "./e2e",
  // Keep all artifacts under e2e/ (gitignored) so nothing leaks into the repo.
  outputDir: "./e2e/.artifacts/test-results",
  // Specs publish to Redis + assert async UI reactions; give each room but bound it.
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never", outputFolder: "./e2e/.artifacts/report" }]]
    : [["list"]],
  use: {
    baseURL: BASE_URL,
    headless: true,
    viewport: { width: 1280, height: 900 },
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // The existing `dev` script runs Vite with the `/api` proxy (vite.config.ts);
    // `--strictPort` makes a port clash fail loudly instead of drifting.
    command: `npm run dev -- --port ${WEB_PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { KINORA_API_TARGET: API_TARGET },
  },
});
