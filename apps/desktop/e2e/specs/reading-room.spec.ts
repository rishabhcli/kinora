import { test, expect } from "../fixtures/test";

// The reading room: open a book, the warm-up settles into the scroll-film
// engine, scrolling scrubs the (Ken-Burns fallback) film, and Escape / Back
// close it. With KINORA_LIVE_VIDEO off + the client AI-film toggle off, NO live
// generation happens — the film pane plays a bundled fallback mp4. These specs
// run frozen (video hidden) so they're deterministic, asserting on DOM state
// (the <video> element, the scroll container) rather than pixels.

test.describe("reading room", () => {
  test("opens a book into the reading dialog", async ({ app, home }) => {
    const room = await home.openBook(/The Frog-King/i);
    await expect(room.dialog).toBeVisible();
    expect(await room.title()).toMatch(/Reading/i);
  });

  test("settles into the scroll-film reading view", async ({ app, home }) => {
    const room = await home.openBook();
    await room.waitUntilReading();
    await expect(room.scrollContainer.first()).toBeVisible();
  });

  test("mounts a film pane <video> element (fallback Ken-Burns)", async ({ app, home }) => {
    const room = await home.openBook();
    await room.waitUntilReading();
    await expect.poll(() => room.videoCount(), { timeout: 15_000 }).toBeGreaterThan(0);
  });

  test("scrolling the text scrubs without errors", async ({ app, home, page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const room = await home.openBook();
    await room.waitUntilReading();
    await room.scrollToFraction(0.5);
    await room.scrollToFraction(0.9);
    await room.scrollToFraction(0.0);
    expect(errors, errors.join("\n")).toEqual([]);
  });

  test("closes with the Escape key", async ({ app, home }) => {
    const room = await home.openBook();
    await room.waitUntilReading();
    await room.closeWithEscape();
  });

  test("closes with the Back button", async ({ app, home }) => {
    const room = await home.openBook();
    await room.waitUntilReading();
    await room.closeWithBackButton();
  });

  test("exposes the AI-film toggle (off → no live generation)", async ({ app, home }) => {
    const room = await home.openBook();
    await room.waitUntilReading();
    await expect(room.aiFilmToggle.first()).toBeVisible();
  });

  test("opens the reading-settings popover", async ({ app, home, page }) => {
    const room = await home.openBook();
    await room.waitUntilReading();
    await room.openSettings();
    await expect(page.getByRole("group", { name: /reading settings/i })).toBeVisible({
      timeout: 10_000,
    });
  });
});
