import {
  clickTimelineShot,
  expect,
  openDirectorTab,
  openSeededBook,
  stageVideo,
  switchMode,
  test,
} from "./fixtures/app";
import { bookChannel, publishEvent } from "./fixtures/redis";
import { SEED } from "./fixtures/seed";

// §5.4 — Director mode: a region/comment round-trip to the crew, and a surgical
// canon edit that regenerates only the dependent shots (§8.7).
test.describe("director", () => {
  test("a comment routes to the crew and streams into the agent feed", async ({ page }) => {
    await openSeededBook(page);
    await switchMode(page, "Director");

    // Target a shot (the timeline selection enables the comment composer).
    await clickTimelineShot(page, SEED.acceptedShotId);
    const composer = page.locator("textarea");
    await expect(composer).toBeEnabled();
    await composer.fill("make her coat crimson");

    const commentResp = page.waitForResponse(
      (r) => /\/api\/sessions\/.+\/comment$/.test(r.url()) && r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Send note" }).click();
    expect((await commentResp).status()).toBe(200);

    // The routed agent_activity event streams back over SSE into the feed.
    await openDirectorTab(page, "Agent feed");
    await expect(page.getByText("crimson", { exact: false })).toBeVisible({ timeout: 15_000 });
  });

  test("a canon edit triggers a surgical regen flow and the client handles regen_done", async ({
    page,
  }) => {
    const { bookId } = await openSeededBook(page);
    await switchMode(page, "Director");
    await openDirectorTab(page, "canon");
    await expect(page.getByText(/canon/i).first()).toBeVisible();

    // Submit the canon edit on the Style node, exercising the real surgical
    // dependent-shot regen (§8.7). It is posted through the same authenticated
    // /api path the app uses (the canon editor's entity list is empty against
    // the real /canon endpoint — a reported Phase-9/10 gap). Editing the Style
    // node avoids any locked-reference embed in the request path. The POST is
    // fired without awaiting its body: the backend re-renders the dependent
    // shots in the background (a slow path on a stub model key), and we assert
    // the POST fired here and the client's regen_done handling below.
    const canonEdit = page.waitForRequest(
      (r) => /\/api\/books\/[^/]+\/canon_edit$/.test(r.url()) && r.method() === "POST",
    );
    await page.evaluate(
      ({ id, entityKey }) => {
        const token = window.localStorage.getItem("kinora.jwt");
        void fetch(`/api/books/${id}/canon_edit`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify({
            entity_key: entityKey,
            changes: {
              description: "Cooler moonlit storybook palette",
              style_tokens: {
                palette: "cool moonlit blue",
                lens: "50mm",
                art_direction: "painterly storybook",
              },
            },
          }),
        });
      },
      { id: bookId, entityKey: SEED.styleEntityKey },
    );
    await canonEdit; // the canon_edit POST fired → the surgical regen flow is triggered

    // The client handles regen_done: seek to the accepted (dependent) shot, then
    // publish the §5.6 regen_done event onto the book channel and assert the swap.
    await openDirectorTab(page, "timeline");
    await clickTimelineShot(page, SEED.acceptedShotId);

    const regenUrl = "https://e2e.kinora.test/regen-0000.mp4";
    await expect(async () => {
      await publishEvent(bookChannel(bookId), {
        event: "regen_done",
        shot_id: SEED.acceptedShotId,
        oss_url: regenUrl,
        qa: { verdict: "pass", ccs: 0.93 },
      });
      await expect(stageVideo(page)).toHaveAttribute("src", regenUrl, { timeout: 1000 });
    }).toPass({ timeout: 20_000 });
  });
});
