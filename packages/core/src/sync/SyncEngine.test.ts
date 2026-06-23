import { afterEach, describe, expect, it, vi } from "vitest";

import type { ShotResponse } from "../api/types";
import type { SyncSegment } from "../events";
import { SyncEngine } from "./SyncEngine";

const shots: ShotResponse[] = [
  {
    shot_id: "a",
    status: "accepted",
    source_span: { page: 0, word_range: [0, 49] },
    duration_s: 5,
    clip_url: "clipA",
  },
  {
    shot_id: "b",
    status: "accepted",
    source_span: { page: 1, word_range: [50, 99] },
    duration_s: 8,
    clip_url: "clipB",
  },
];

const segA: SyncSegment = {
  shot_id: "a",
  video_start_s: 0,
  video_end_s: 5,
  page: 0,
  page_turn_at_s: 4.8,
  words: [
    { word_index: 5, text: "x", t_start: 0, t_end: 1, bbox: null },
    { word_index: 9, text: "y", t_start: 1, t_end: 2, bbox: null },
  ],
};

afterEach(() => {
  vi.useRealTimers();
});

describe("SyncEngine", () => {
  it("resolves the current shot + clip from a scroll position", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.reportScroll(70, 1000);
    expect(engine.getSnapshot().currentShotId).toBe("b");
    expect(engine.getSnapshot().currentClipUrl).toBe("clipB");
    expect(engine.getSnapshot().focusWord).toBe(70);
  });

  it("lets video drive the karaoke highlight only after the scroll grace", () => {
    const engine = new SyncEngine({ graceMs: 1000 });
    engine.setShots(shots);
    engine.ingestClip(segA, "clipA");
    engine.reportScroll(10, 0); // scroll owns until t=1000

    engine.reportVideoTime(1.5, 500); // within grace -> ignored
    expect(engine.getSnapshot().highlightWordIndex).toBeNull();

    engine.reportVideoTime(1.5, 2000); // grace expired -> video owns
    expect(engine.getSnapshot().owner).toBe("video");
    expect(engine.getSnapshot().highlightWordIndex).toBe(9);
  });

  it("debounces intent and reports the latest word + velocity", () => {
    vi.useFakeTimers();
    const onIntent = vi.fn();
    const engine = new SyncEngine({ intentDebounceMs: 200, callbacks: { onIntent } });
    engine.setShots(shots);
    engine.reportScroll(10, 0);
    engine.reportScroll(12, 100);
    expect(onIntent).not.toHaveBeenCalled();
    vi.advanceTimersByTime(250);
    expect(onIntent).toHaveBeenCalledTimes(1);
    expect(onIntent).toHaveBeenCalledWith(expect.objectContaining({ focusWord: 12 }));
  });

  it("seek emits immediately and moves the playhead", () => {
    const onSeek = vi.fn();
    const engine = new SyncEngine({ callbacks: { onSeek } });
    engine.setShots(shots);
    engine.seek(60, 0);
    expect(onSeek).toHaveBeenCalledWith(60);
    expect(engine.getSnapshot().currentShotId).toBe("b");
    expect(engine.getSnapshot().focusWord).toBe(60);
  });

  it("swapClipUrl hot-swaps the on-screen shot but stays quiet for off-screen ones", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.reportScroll(10, 0); // current shot is "a"
    expect(engine.getSnapshot().currentClipUrl).toBe("clipA");

    // regen_done for the shot on screen -> the stage source swaps in place.
    engine.swapClipUrl("a", "clipA-v2");
    expect(engine.getSnapshot().currentClipUrl).toBe("clipA-v2");

    // regen_done for an off-screen shot -> no visible change now...
    engine.swapClipUrl("b", "clipB-v2");
    expect(engine.getSnapshot().currentClipUrl).toBe("clipA-v2");
    // ...but the new take is used once the playhead reaches it.
    engine.reportScroll(70, 100);
    expect(engine.getSnapshot().currentClipUrl).toBe("clipB-v2");
  });

  it("notifies subscribers on change", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    const listener = vi.fn();
    const unsub = engine.subscribe(listener);
    engine.reportScroll(20, 0);
    expect(listener).toHaveBeenCalled();
    unsub();
    const before = listener.mock.calls.length;
    engine.reportScroll(25, 100);
    expect(listener.mock.calls.length).toBe(before);
  });
});

// Shots with beats but no rendered clip yet — the speculative/cold zones (§4.4).
const ladderShots: ShotResponse[] = [
  {
    shot_id: "s1",
    beat_id: "beat1",
    status: "planned",
    source_span: { page: 1, word_range: [0, 49] },
    duration_s: 5,
  },
  {
    shot_id: "s2",
    beat_id: "beat2",
    status: "planned",
    source_span: { page: 2, word_range: [50, 99] },
    duration_s: 6,
  },
];

const segS1: SyncSegment = {
  shot_id: "s1",
  video_start_s: 0,
  video_end_s: 5,
  page: 1,
  page_turn_at_s: 4.8,
  words: [],
};

describe("SyncEngine degradation ladder (§12.4)", () => {
  it("climbs the rungs as assets arrive: floor → illustration → keyframe → full video", () => {
    const engine = new SyncEngine();
    engine.setShots(ladderShots);
    engine.reportScroll(10, 0); // on s1 / beat1, nothing rendered

    expect(engine.getSnapshot().currentBeatId).toBe("beat1");
    expect(engine.getSnapshot().currentStage).toBe("audio_text_only");
    expect(engine.getSnapshot().currentClipUrl).toBeNull();

    // The book's own page image arrives -> the illustration rung.
    engine.setPageIllustration(1, "page1.png");
    expect(engine.getSnapshot().currentStage).toBe("illustration");
    expect(engine.getSnapshot().currentIllustrationUrl).toBe("page1.png");

    // A speculative keyframe outranks the illustration -> the Ken-Burns bridge.
    engine.ingestKeyframe("beat1", "kf1.png");
    expect(engine.getSnapshot().currentStage).toBe("keyframe_ken_burns");
    expect(engine.getSnapshot().currentKeyframeUrl).toBe("kf1.png");

    // The committed clip lands -> top rung, full video.
    engine.ingestClip(segS1, "clipS1");
    expect(engine.getSnapshot().currentStage).toBe("full_video");
    expect(engine.getSnapshot().currentClipUrl).toBe("clipS1");
  });

  it("routes keyframes to the right beat and a backward seek is an instant cache hit", () => {
    const engine = new SyncEngine();
    engine.setShots(ladderShots);
    engine.ingestKeyframe("beat1", "kf1.png");
    engine.reportScroll(10, 0); // beat1 -> keyframe is ready
    expect(engine.getSnapshot().currentStage).toBe("keyframe_ken_burns");

    // Forward into an unkeyframed beat -> we fall back to the floor.
    engine.reportScroll(60, 100); // beat2, no still
    expect(engine.getSnapshot().currentBeatId).toBe("beat2");
    expect(engine.getSnapshot().currentStage).toBe("audio_text_only");

    // Backward seek to beat1 -> its keyframe is still cached, served instantly.
    engine.seek(10, 200);
    expect(engine.getSnapshot().currentStage).toBe("keyframe_ken_burns");
    expect(engine.getSnapshot().currentKeyframeUrl).toBe("kf1.png");
  });

  it("an asset for an off-screen beat is a silent cache write (no emit)", () => {
    const engine = new SyncEngine();
    engine.setShots(ladderShots);
    engine.reportScroll(10, 0); // on beat1
    const listener = vi.fn();
    engine.subscribe(listener);

    engine.ingestKeyframe("beat2", "kf2.png"); // off-screen -> cached, no render
    expect(listener).not.toHaveBeenCalled();
    expect(engine.getSnapshot().currentStage).toBe("audio_text_only");

    // It is used the moment the playhead reaches it (still a cache hit).
    engine.reportScroll(60, 100);
    expect(engine.getSnapshot().currentKeyframeUrl).toBe("kf2.png");
    expect(engine.getSnapshot().currentStage).toBe("keyframe_ken_burns");
  });

  it("budget_low steps down; a committed clip refilling the buffer steps back up", () => {
    const engine = new SyncEngine();
    engine.setShots(ladderShots);
    engine.reportScroll(10, 0);

    engine.noteBudgetLow(40);
    expect(engine.getSnapshot().underBudgetPressure).toBe(true);
    expect(engine.getSnapshot().budgetRemaining).toBe(40);

    // A fresh clip means the buffer is refilling -> pressure released (§12.4 up).
    engine.ingestClip(segS1, "clipS1");
    expect(engine.getSnapshot().underBudgetPressure).toBe(false);
    expect(engine.getSnapshot().currentStage).toBe("full_video");
  });

  it("buffer_state steps the ladder back up only once the buffer clears the low watermark", () => {
    const engine = new SyncEngine();
    engine.setShots(ladderShots);
    engine.reportScroll(10, 0);
    engine.noteBudgetLow(30);

    // Still draining below the low watermark -> stay stepped down (just track it).
    engine.noteBufferState({ committedSecondsAhead: 12, lowWatermarkS: 25 });
    expect(engine.getSnapshot().committedSecondsAhead).toBe(12);
    expect(engine.getSnapshot().underBudgetPressure).toBe(true);

    // Refilled past the low watermark -> step back up (§4.5/§12.4).
    engine.noteBufferState({ committedSecondsAhead: 40, lowWatermarkS: 25 });
    expect(engine.getSnapshot().committedSecondsAhead).toBe(40);
    expect(engine.getSnapshot().underBudgetPressure).toBe(false);
  });

  it("upcomingStillUrls lists the current + next beats' stills for prefetch", () => {
    const engine = new SyncEngine();
    engine.setShots(ladderShots);
    engine.ingestKeyframe("beat1", "kf1.png");
    engine.setPageIllustration(2, "page2.png"); // s2 is page 2, no keyframe
    engine.reportScroll(10, 0); // on s1 / beat1

    const urls = engine.upcomingStillUrls(1);
    expect(urls).toContain("kf1.png"); // current beat's keyframe
    expect(urls).toContain("page2.png"); // next shot falls back to its illustration
  });

  it("dropKeyframe evicts a broken still and falls through to the next rung", () => {
    const engine = new SyncEngine();
    engine.setShots(ladderShots);
    engine.ingestKeyframe("beat1", "kf1.png");
    engine.setPageIllustration(1, "page1.png");
    engine.reportScroll(10, 0);
    expect(engine.getSnapshot().currentStage).toBe("keyframe_ken_burns");

    engine.dropKeyframe("beat1"); // its URL 404'd / expired
    expect(engine.getSnapshot().currentStage).toBe("illustration");
    expect(engine.getSnapshot().currentKeyframeUrl).toBeNull();
  });
});
