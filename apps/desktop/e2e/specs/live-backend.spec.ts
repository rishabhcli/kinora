import { test, expect, LIVE_ENABLED } from "../fixtures/test";

// Real-backend smoke. Gated behind KINORA_E2E_LIVE=1 AND the mock being OFF
// (KINORA_E2E_MOCK=0), so it NEVER runs in the default hermetic CI path. Point
// the dev server at a running FastAPI stack (VITE_KINORA_API_URL) with the demo
// book seeded (`make seed-demo`). KINORA_LIVE_VIDEO MUST stay OFF — this only
// verifies the renderer talks to a live API + lists real books; it does not
// trigger or assert on live Wan generation.
//
//   KINORA_E2E_MOCK=0 KINORA_E2E_LIVE=1 \
//     npx playwright test -c e2e/playwright.e2e.config.ts e2e/specs/live-backend.spec.ts

test.describe("live backend smoke", () => {
  test.skip(!LIVE_ENABLED, "set KINORA_E2E_LIVE=1 (and KINORA_E2E_MOCK=0) to run against a real stack");

  test("logs in against the real API and lists books", async ({ login, home }) => {
    await login.fillCredentials("demo@kinora.local", "demo-password-123");
    await login.open();
    await login.signIn();
    await home.waitUntilReady();
    await expect(home.firstCard()).toBeVisible({ timeout: 30_000 });
  });

  test("opens a real book into the reading room (fallback film, no live Wan)", async ({
    login,
    home,
  }) => {
    await login.open();
    await login.signIn();
    await home.waitUntilReady();
    const room = await home.openBook();
    await expect(room.dialog).toBeVisible({ timeout: 30_000 });
    await room.waitUntilReading();
  });
});
