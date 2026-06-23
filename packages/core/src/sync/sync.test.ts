import { describe, expect, it } from "vitest";

import type { ShotResponse } from "../api/types";
import type { SyncSegment } from "../events";
import { parseSessionEvent } from "../events";
import {
  activeSyncWordIndexAt,
  buildTimeline,
  highlightedWordIndexAt,
  shotIndexForWord,
  shouldTurnPage,
} from "./timeline";
import { VelocityTracker } from "./velocity";

describe("VelocityTracker", () => {
  it("estimates words/sec from steady reading", () => {
    const v = new VelocityTracker({ halfLifeMs: 1000 });
    v.sample(0, 0);
    let last = 0;
    for (let t = 200; t <= 4000; t += 200) {
      last = v.sample((t / 1000) * 4, t); // 4 words/sec
    }
    expect(last).toBeGreaterThan(3);
    expect(last).toBeLessThan(5);
  });

  it("clamps backward reading to >= 0", () => {
    const v = new VelocityTracker();
    v.sample(100, 0);
    expect(v.sample(0, 500)).toBeGreaterThanOrEqual(0);
  });
});

describe("timeline", () => {
  const shots: ShotResponse[] = [
    {
      shot_id: "b",
      status: "accepted",
      source_span: { page: 1, word_range: [50, 99] },
      duration_s: 8,
      clip_url: "u2",
    },
    {
      shot_id: "a",
      status: "accepted",
      source_span: { page: 0, word_range: [0, 49] },
      duration_s: 10,
      clip_url: "u1",
    },
  ];

  it("sorts by start word and assigns cumulative offsets", () => {
    const tl = buildTimeline(shots);
    expect(tl.map((s) => s.shotId)).toEqual(["a", "b"]);
    expect(tl[0]?.videoStartS).toBe(0);
    expect(tl[1]?.videoStartS).toBe(10);
  });

  it("resolves a word to its shot", () => {
    const tl = buildTimeline(shots);
    expect(shotIndexForWord(tl, 25)).toBe(0);
    expect(shotIndexForWord(tl, 70)).toBe(1);
    expect(shotIndexForWord(tl, -5)).toBe(-1);
  });
});

describe("sync segment lookups", () => {
  const seg: SyncSegment = {
    shot_id: "a",
    video_start_s: 0,
    video_end_s: 5,
    page: 0,
    page_turn_at_s: 4.8,
    words: [
      { word_index: 0, text: "the", t_start: 0, t_end: 0.5, bbox: null },
      { word_index: 1, text: "cat", t_start: 0.5, t_end: 1.2, bbox: [0, 0, 0.1, 0.05] },
      { word_index: 2, text: "sat", t_start: 1.2, t_end: 2.0, bbox: null },
    ],
  };

  it("finds the active word and its global index", () => {
    expect(activeSyncWordIndexAt(seg, 0.7)).toBe(1);
    expect(highlightedWordIndexAt(seg, 0.7)).toBe(1);
    expect(activeSyncWordIndexAt(seg, -1)).toBe(-1);
  });

  it("turns the page near the end", () => {
    expect(shouldTurnPage(seg, 4.0)).toBe(false);
    expect(shouldTurnPage(seg, 4.9)).toBe(true);
  });
});

describe("parseSessionEvent", () => {
  it("parses clip_ready with a sync segment", () => {
    const evt = parseSessionEvent({
      event: "clip_ready",
      shot_id: "a",
      oss_url: "https://cdn/clip.mp4",
      sync_segment: {
        shot_id: "a",
        video_start_s: 0,
        video_end_s: 5,
        page: 0,
        page_turn_at_s: 4.8,
        words: [],
      },
    });
    expect(evt?.event).toBe("clip_ready");
  });

  it("returns null for unknown junk", () => {
    expect(parseSessionEvent({ event: "nope" })).toBeNull();
    expect(parseSessionEvent(42)).toBeNull();
  });
});
