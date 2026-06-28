import { test, expect } from "../fixtures/test";
import { freezeForSnapshot, settle } from "../support/stabilize";
import { HOOK } from "../support/selectors";

// Visual-regression snapshots. The renderer is heavily animated + media-driven,
// so every snapshot runs FROZEN (reduced-motion, animations off, clock + random
// pinned, <video> hidden) and masks the genuinely-dynamic regions (ambient
// canvases, the time-based greeting, the film pane). Baselines are committed
// under e2e/visual/__screenshots__/ and updated with `--update-snapshots`.
//
// First run on a new machine has NO baseline → Playwright writes one and the
// test is reported as "did not run / created". CI should run once to seed, then
// assert on subsequent runs. Tolerances live in playwright.e2e.config.ts
// (maxDiffPixelRatio 0.02) to absorb sub-pixel AA across platforms.

test.describe("visual regression", () => {
  test("login screen", async ({ login, page }) => {
    await login.open();
    await settle(page);
    await freezeForSnapshot(page);
    await expect(page).toHaveScreenshot("login.png", {
      // The hero side runs an aurora/vignette animation; mask it.
      mask: [page.locator(".login-hero")],
    });
  });

  test("home shell", async ({ app, page }) => {
    await settle(page);
    await freezeForSnapshot(page);
    await expect(page).toHaveScreenshot("home.png", {
      mask: [
        // Time-based greeting + any ambient background canvas + cover art.
        page.locator("canvas"),
        page.getByText(/good (morning|afternoon|evening|night)/i),
        page.locator(HOOK.bookCover),
      ],
      maxDiffPixelRatio: 0.05,
    });
  });

  test("library (harness)", async ({ library, page }) => {
    await library.openHarness();
    await settle(page);
    await freezeForSnapshot(page);
    await expect(page).toHaveScreenshot("library.png", {
      mask: [page.locator("canvas"), page.locator(HOOK.bookCover)],
      maxDiffPixelRatio: 0.05,
    });
  });

  test("director studio (harness)", async ({ page }) => {
    await page.goto("/e2e/harness/director.html");
    await expect(page.locator('[role="tablist"]')).toBeVisible({ timeout: 15_000 });
    await settle(page);
    await freezeForSnapshot(page);
    await expect(page).toHaveScreenshot("director.png", {
      mask: [page.locator("canvas"), page.locator("video")],
      maxDiffPixelRatio: 0.05,
    });
  });
});
