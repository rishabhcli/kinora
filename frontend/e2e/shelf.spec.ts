import { expect, login, test } from "./fixtures/app";
import { SEED } from "./fixtures/seed";

// §5.1 — the shelf: the seeded book shows as ready and opens its workspace.
test.describe("shelf", () => {
  test("the seeded book appears ready and opens the workspace", async ({ page }) => {
    await login(page);

    const card = page.locator('a[href^="/book/"]').filter({ hasText: SEED.bookTitle });
    await expect(card).toBeVisible();
    // A ready book card shows "Ready · N pages" and is a navigable link.
    await expect(card).toContainText("Ready");

    await card.click();
    await page.waitForURL(/\/book\//);

    // Landing in the workspace renders the real rasterised first page.
    await expect(page.getByAltText("Page 1")).toBeVisible({ timeout: 30_000 });
  });
});
