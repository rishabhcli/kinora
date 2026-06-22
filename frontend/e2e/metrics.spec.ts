import { expect, openSeededBook, test } from "./fixtures/app";

// §13 — the metrics panel: the buffer-occupancy sawtooth + the crew-vs-baseline
// comparison bars, served by the real /api/eval endpoints (zero video).
test.describe("metrics", () => {
  test("renders the buffer sawtooth and crew-vs-baseline bars", async ({ page }) => {
    await openSeededBook(page);

    await page.getByRole("button", { name: "Metrics" }).click();
    await expect(page.getByRole("heading", { name: "Metrics" })).toBeVisible();

    // The §4.10 buffer-occupancy sawtooth (recomputed live from the session's
    // source-span index, zero video-seconds) renders as a Recharts line chart.
    await expect(page.getByText("Buffer occupancy")).toBeVisible();
    await expect(page.locator(".recharts-surface").first()).toBeVisible({ timeout: 15_000 });

    // The crew-vs-baseline comparison bars (from the cached §13 eval report).
    await expect(
      page.getByRole("heading", { name: "Character consistency (CCS)" }),
    ).toBeVisible({ timeout: 15_000 });
    await expect(
      page.getByRole("heading", { name: "Accepted-footage efficiency" }),
    ).toBeVisible();
    await expect(page.getByText("crew wins").first()).toBeVisible();
  });
});
