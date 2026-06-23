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
