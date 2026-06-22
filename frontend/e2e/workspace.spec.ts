import { expect, openSeededBook, test } from "./fixtures/app";

// §5.2/§5.3 — the workspace left pane: page images + word-box overlay, scroll
// drives a debounced intent update, and the buffer indicator is present.
test.describe("workspace", () => {
  test("renders page + word boxes, shows the buffer meter, and fires intent on scroll", async ({
    page,
  }) => {
    await openSeededBook(page);

    // Left pane: the rasterised page image and the karaoke/word-box overlay.
    await expect(page.getByAltText("Page 1")).toBeVisible();
    await expect(page.locator("[data-word-index]").first()).toBeAttached();
    expect(await page.locator("[data-word-index]").count()).toBeGreaterThan(0);

    // The deliberately-subtle buffer hairline (Viewer mode, §5.3).
    await expect(page.getByRole("meter", { name: "Generation buffer" })).toBeVisible();

    // Scrolling the reader updates the focus word and pushes a debounced
    // POST /api/sessions/:id/intent (the §4 generation-on-scroll trigger).
    const intent = page.waitForRequest(
      (r) => /\/api\/sessions\/.+\/intent$/.test(r.url()) && r.method() === "POST",
    );
    const scroller = page
      .locator("div.overflow-y-auto")
      .filter({ has: page.getByAltText("Page 1") });
    await scroller.hover();
    await page.mouse.wheel(0, 500);
    await page.mouse.wheel(0, 600);
    // Fallback nudge in case the wheel target differs across engines.
    await scroller.evaluate((el) => {
      el.scrollTop += 400;
      el.dispatchEvent(new Event("scroll"));
    });
    await intent;
  });
});
