import {
  clickTimelineShot,
  expect,
  openSeededBook,
  stageVideo,
  switchMode,
  test,
} from "./fixtures/app";
import { publishEvent, sessionChannel } from "./fixtures/redis";
import { SEED } from "./fixtures/seed";

// §5.6 — generation events: publishing onto the session's Redis pub/sub channel
// (the real §5.6 fan-out the SSE stream consumes) makes the video stage react.
test.describe("events", () => {
  test("clip_ready hot-swaps the video source", async ({ page }) => {
    const { sessionId } = await openSeededBook(page);

    // Director mode exposes the shot timeline; selecting a clip-less (planned)
    // shot puts the stage into the Ken-Burns bridge with no <video> yet.
    await switchMode(page, "Director");
    await clickTimelineShot(page, SEED.plannedShotId);

    const sentinel = "https://e2e.kinora.test/clip-0001.mp4";
    const syncSegment = {
      shot_id: SEED.plannedShotId,
      video_start_s: 0,
      video_end_s: 5,
      page: 1,
      page_turn_at_s: 4.8,
      words: [{ word_index: 10, text: "She", t_start: 0.1, t_end: 0.4, bbox: [0.1, 0.3, 0.04, 0.02] }],
    };

    // Re-publish until the SSE subscription is live and the engine hot-swaps the
    // source in (idempotent — registerClip just sets the same URL).
    await expect(async () => {
      await publishEvent(sessionChannel(sessionId), {
        event: "clip_ready",
        shot_id: SEED.plannedShotId,
        oss_url: sentinel,
        sync_segment: syncSegment,
      });
      await expect(stageVideo(page)).toHaveAttribute("src", sentinel, { timeout: 1000 });
    }).toPass({ timeout: 20_000 });
  });

  test("keyframe_ready provides the Ken-Burns bridge still", async ({ page }) => {
    const { sessionId } = await openSeededBook(page);
    await switchMode(page, "Director");

    const keyframe = "https://e2e.kinora.test/keyframe-0002.png";
    // The keyframe is cached on arrival; re-seeking the shot reads it into the
    // bridge. Re-publish + re-seek until the cached still paints the bridge img.
    await expect(async () => {
      await publishEvent(sessionChannel(sessionId), {
        event: "keyframe_ready",
        beat_id: "beat_0002",
        shot_id: SEED.keyframeShotId,
        oss_url: keyframe,
      });
      await clickTimelineShot(page, SEED.keyframeShotId);
      await expect(page.locator("img.ken-burns-bridge")).toHaveAttribute("src", keyframe, {
        timeout: 1000,
      });
    }).toPass({ timeout: 20_000 });
  });
});
