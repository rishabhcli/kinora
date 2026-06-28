import { test, expect, MOCK_ENABLED } from "../fixtures/test";
import { enableAiFilm } from "../support/flags";

// Live film-sync via the mocked SSE stream. This exercises the *whole* live
// override path WITHOUT any real backend or Wan credits:
//   sign in (mock 200 → token) → AI-film toggle ON → open seed book →
//   useFilmSession creates a session + opens the (faked) EventSource →
//   we push buffer_state / clip_ready frames and assert the UI reflects them.
//
// KINORA_LIVE_VIDEO is irrelevant here — that's a *backend* gate; this proves the
// *renderer's* live-state plumbing. Skipped entirely when the mock is disabled
// (the real backend won't emit frames on demand, and may have live video off).

test.describe("film sync (mocked SSE)", () => {
  test.skip(!MOCK_ENABLED, "requires the deterministic API + EventSource mock");

  test.beforeEach(async ({ page }) => {
    await enableAiFilm(page);
  });

  test("a buffer_state frame surfaces the buffered-ahead indicator", async ({
    login,
    home,
    api,
    page,
  }) => {
    await login.open();
    await login.signIn();
    await home.waitUntilReady();
    const room = await home.openBook(/The Frog-King/i);
    await room.waitUntilReading();

    // The SSE stream opens once the live session is created.
    await expect
      .poll(() => page.evaluate(() => (window as any).__kinoraOpenStreams?.() ?? 0), {
        timeout: 20_000,
      })
      .toBeGreaterThan(0);

    await api!.pushEvent({
      event: "buffer_state",
      committed_seconds_ahead: 14,
      bursting: false,
      idle: false,
      budget_remaining_s: 0,
    });

    await expect(page.getByText(/buffered\s*14s\s*ahead/i).first()).toBeVisible({
      timeout: 10_000,
    });
  });

  test("a clip_ready frame is accepted without page errors", async ({
    login,
    home,
    api,
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await login.open();
    await login.signIn();
    await home.waitUntilReady();
    const room = await home.openBook(/The Frog-King/i);
    await room.waitUntilReading();

    await expect
      .poll(() => page.evaluate(() => (window as any).__kinoraOpenStreams?.() ?? 0), {
        timeout: 20_000,
      })
      .toBeGreaterThan(0);

    await api!.pushEvent({
      event: "clip_ready",
      shot_id: "shot-fk-1",
      oss_url: "/generated/film-02.mp4",
      video_seconds: 6,
    });
    await page.waitForTimeout(500);
    expect(errors, errors.join("\n")).toEqual([]);
  });

  test("agent_activity frames feed the crew ticker without errors", async ({
    login,
    home,
    api,
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await login.open();
    await login.signIn();
    await home.waitUntilReady();
    const room = await home.openBook(/The Frog-King/i);
    await room.waitUntilReading();

    await expect
      .poll(() => page.evaluate(() => (window as any).__kinoraOpenStreams?.() ?? 0), {
        timeout: 20_000,
      })
      .toBeGreaterThan(0);

    await api!.pushEvent({ event: "agent_activity", agent: "Cinematographer", message: "Framing the well" });
    await page.waitForTimeout(300);
    expect(errors, errors.join("\n")).toEqual([]);
  });
});
