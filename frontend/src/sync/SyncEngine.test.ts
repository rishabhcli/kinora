import { afterEach, describe, expect, it, vi } from "vitest";

import type { Shot, SyncSegment } from "../api/types";
import { SyncEngine, type SyncEngineConfig } from "./SyncEngine";

function makeEngine(overrides: Partial<SyncEngineConfig> = {}) {
  const pushIntent = vi.fn();
  const postSeek = vi.fn();
  const engine = new SyncEngine({ sessionId: "sess", pushIntent, postSeek, ...overrides });
  return { engine, pushIntent, postSeek };
}

const shots: Shot[] = [
  {
    shot_id: "s1",
    beat_id: "b1",
    scene_id: "sc1",
    status: "keyframed",
    source_span: { page: 1, word_range: [0, 30] },
    est_duration_s: 5,
  },
  {
    shot_id: "s2",
    beat_id: "b2",
    scene_id: "sc1",
    status: "keyframed",
    source_span: { page: 2, word_range: [30, 60] },
    est_duration_s: 5,
  },
];

afterEach(() => {
  vi.useRealTimers();
});

describe("SyncEngine — control-owner token (prevents the two-way binding loop)", () => {
  it("manual scroll grabs ownership and suppresses the video page-turn during the 1.2s grace", () => {
    const { engine } = makeEngine();
    engine.onScrollInput(10, 0);
    expect(engine.getSnapshot().owner).toBe("scroll");

    engine.onVideoTime(1.0, 500); // inside grace
    expect(engine.getSnapshot().owner).toBe("scroll");

    engine.onVideoTime(1.0, 1199); // still inside grace
    expect(engine.getSnapshot().owner).toBe("scroll");

    engine.onVideoTime(1.0, 1300); // past grace → video reclaims
    expect(engine.getSnapshot().owner).toBe("video");
  });

  it("honours a configurable grace window", () => {
    const { engine } = makeEngine({ graceMs: 500 });
    engine.onScrollInput(10, 0);
    engine.onVideoTime(1, 400);
    expect(engine.getSnapshot().owner).toBe("scroll");
    engine.onVideoTime(1, 600);
    expect(engine.getSnapshot().owner).toBe("video");
  });
});

describe("SyncEngine — EWMA velocity clamp", () => {
  it("clamps a single flick to the ceiling (3x default)", () => {
    const { engine } = makeEngine();
    engine.onScrollInput(0, 0);
    engine.onScrollInput(100_000, 50);
    expect(engine.getSnapshot().velocity).toBe(12);
  });

  it("starts at the default 4 wps", () => {
    const { engine } = makeEngine();
    engine.onScrollInput(0, 0);
    expect(engine.getSnapshot().velocity).toBe(4);
  });
});

describe("SyncEngine — debounced intent (200ms settle)", () => {
  it("coalesces rapid scroll samples into a single intent push", () => {
    vi.useFakeTimers();
    const { engine, pushIntent } = makeEngine();
    engine.onScrollInput(5, 0);
    engine.onScrollInput(8, 10);
    engine.onScrollInput(11, 20);
    expect(pushIntent).not.toHaveBeenCalled();
    vi.advanceTimersByTime(199);
    expect(pushIntent).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(pushIntent).toHaveBeenCalledTimes(1);
    expect(pushIntent).toHaveBeenCalledWith(
      expect.objectContaining({ focus_word: 11, mode: "viewer" }),
    );
  });

  it("respects a custom debounce window", () => {
    vi.useFakeTimers();
    const { engine, pushIntent } = makeEngine({ debounceMs: 50 });
    engine.onScrollInput(5, 0);
    vi.advanceTimersByTime(50);
    expect(pushIntent).toHaveBeenCalledTimes(1);
  });
});

describe("SyncEngine — seek bridge + clip hot-swap", () => {
  it("bridges a seek with the keyframe under Ken-Burns, then swaps in the real clip", () => {
    const { engine, postSeek } = makeEngine();
    engine.setShots(shots);
    engine.registerKeyframe("b1", "kf1.png");

    engine.seek(5, 0);
    let snap = engine.getSnapshot();
    expect(snap.owner).toBe("scroll");
    expect(snap.currentShotId).toBe("s1");
    expect(snap.bridging).toBe(true);
    expect(snap.bridgeKeyframeUrl).toBe("kf1.png");
    expect(snap.videoSrc).toBeNull();
    expect(postSeek).toHaveBeenCalledWith(5);

    const seg: SyncSegment = {
      shot_id: "s1",
      video_start_s: 0,
      video_end_s: 5,
      page: 1,
      page_turn_at_s: 4.8,
      words: [{ word_index: 0, text: "x", t_start: 0, t_end: 1 }],
    };
    engine.registerClip("s1", "clip1.mp4", seg);
    snap = engine.getSnapshot();
    expect(snap.videoSrc).toBe("clip1.mp4");

    // The next shot's clip warms the hidden preload buffer.
    engine.registerClip("s2", "clip2.mp4");
    expect(engine.getSnapshot().preloadSrc).toBe("clip2.mp4");
    // Both contiguous ready clips count toward the committed buffer.
    expect(engine.getSnapshot().committedSecondsAhead).toBeCloseTo(10);
  });

  it("advances to the next shot on a clean boundary (onVideoEnded)", () => {
    const { engine } = makeEngine();
    engine.setShots(shots);
    engine.registerClip("s1", "clip1.mp4");
    engine.registerClip("s2", "clip2.mp4");
    engine.seek(5, 0);
    engine.onVideoEnded();
    const snap = engine.getSnapshot();
    expect(snap.currentShotId).toBe("s2");
    expect(snap.videoSrc).toBe("clip2.mp4");
  });

  it("drops the bridge once real video is rendering", () => {
    const { engine } = makeEngine();
    engine.setShots(shots);
    const seg: SyncSegment = {
      shot_id: "s1",
      video_start_s: 0,
      video_end_s: 5,
      page: 1,
      page_turn_at_s: 4.8,
      words: [{ word_index: 0, text: "x", t_start: 0, t_end: 1 }],
    };
    engine.registerKeyframe("b1", "kf1.png");
    engine.seek(5, 0);
    engine.registerClip("s1", "clip1.mp4", seg);
    expect(engine.getSnapshot().bridging).toBe(true);
    // A video tick past t=0 with the clip playing clears the bridge.
    engine.onVideoTime(0.5, 0);
    expect(engine.getSnapshot().bridging).toBe(false);
  });

  it("regen_done swaps the currently-playing shot", () => {
    const { engine } = makeEngine();
    engine.setShots(shots);
    engine.registerClip("s1", "clip1.mp4");
    engine.seek(5, 0);
    engine.registerRegen("s1", "clip1_v2.mp4");
    expect(engine.getSnapshot().videoSrc).toBe("clip1_v2.mp4");
  });
});

describe("SyncEngine — Viewer mode karaoke + page turn", () => {
  it("drives the active word and page from the sync segment", () => {
    const { engine } = makeEngine();
    engine.setShots(shots);
    const seg: SyncSegment = {
      shot_id: "s1",
      video_start_s: 0,
      video_end_s: 5,
      page: 7,
      page_turn_at_s: 4.8,
      words: [
        { word_index: 0, text: "She", t_start: 0.1, t_end: 0.4 },
        { word_index: 1, text: "ran", t_start: 0.4, t_end: 0.9 },
      ],
    };
    engine.registerClip("s1", "clip1.mp4", seg);
    engine.seek(0, 0);
    // let the grace expire so the video owns the playhead
    engine.onVideoTime(0.5, 2000);
    expect(engine.getSnapshot().owner).toBe("video");
    expect(engine.getSnapshot().activeWordIndex).toBe(1);
    expect(engine.getSnapshot().currentPage).toBe(7);
    engine.onVideoTime(4.9, 2100); // past page_turn_at_s → next page
    expect(engine.getSnapshot().currentPage).toBe(8);
  });
});
