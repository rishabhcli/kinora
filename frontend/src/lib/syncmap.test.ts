import { describe, expect, it } from "vitest";

import type { Shot, SyncMap, SyncSegment } from "../api/types";
import {
  activeWordIndexAt,
  bboxForWord,
  highlightStateForWord,
  localTime,
  seekTargetForWord,
  shotForWord,
  shotIndexForWord,
  shouldTurnPage,
} from "./syncmap";

const segment: SyncSegment = {
  shot_id: "shot_00042",
  video_start_s: 0,
  video_end_s: 5,
  page: 12,
  page_turn_at_s: 4.8,
  words: [
    { word_index: 4501, text: "She", t_start: 0.1, t_end: 0.32, bbox: [0.12, 0.34, 0.04, 0.02] },
    { word_index: 4502, text: "stood", t_start: 0.32, t_end: 0.61, bbox: [0.17, 0.34, 0.06, 0.02] },
    { word_index: 4503, text: "still", t_start: 0.61, t_end: 0.95, bbox: [0.24, 0.34, 0.05, 0.02] },
  ],
};

describe("activeWordIndexAt (sync_segment → highlighted word)", () => {
  it("returns null before the first word starts", () => {
    expect(activeWordIndexAt(segment.words, 0.0)).toBeNull();
  });

  it("maps a time to the word being spoken", () => {
    expect(activeWordIndexAt(segment.words, 0.1)).toBe(4501);
    expect(activeWordIndexAt(segment.words, 0.2)).toBe(4501);
    expect(activeWordIndexAt(segment.words, 0.32)).toBe(4502);
    expect(activeWordIndexAt(segment.words, 0.7)).toBe(4503);
  });

  it("clears after the final word finishes", () => {
    expect(activeWordIndexAt(segment.words, 2.0)).toBeNull();
  });
});

describe("bboxForWord", () => {
  it("returns the normalized box for a word index", () => {
    expect(bboxForWord(segment.words, 4502)).toEqual([0.17, 0.34, 0.06, 0.02]);
    expect(bboxForWord(segment.words, 9999)).toBeNull();
  });
});

describe("highlightStateForWord", () => {
  it("classifies played / active / ahead", () => {
    expect(highlightStateForWord(segment.words[0], 0.05)).toBe("ahead");
    expect(highlightStateForWord(segment.words[0], 0.2)).toBe("active");
    expect(highlightStateForWord(segment.words[0], 0.5)).toBe("played");
  });
});

describe("shouldTurnPage + localTime", () => {
  it("turns at page_turn_at_s", () => {
    expect(shouldTurnPage(segment, 4.7)).toBe(false);
    expect(shouldTurnPage(segment, 4.8)).toBe(true);
  });
  it("offsets by the segment start", () => {
    const stitched: SyncSegment = { ...segment, video_start_s: 10 };
    expect(localTime(stitched, 10.5)).toBeCloseTo(0.5);
  });
});

describe("seekTargetForWord (scroll → video)", () => {
  it("resolves a focus word to a shot + in-shot time", () => {
    const map: SyncMap = { scene_id: "scene_005", segments: [segment] };
    const target = seekTargetForWord(map, 4502);
    expect(target?.shotId).toBe("shot_00042");
    expect(target?.videoTimeS).toBeCloseTo(0.32);
  });
  it("returns null when the word is outside every segment", () => {
    const map: SyncMap = { scene_id: "scene_005", segments: [segment] };
    expect(seekTargetForWord(map, 1)).toBeNull();
  });

  it("treats stitched word times as absolute (no video_start_s double offset)", () => {
    // A stitched segment: it starts 10s into the scene clip and its word times
    // are already absolute scene-clip times (kinora.md §9.6).
    const stitched: SyncMap = {
      scene_id: "scene_005",
      segments: [
        {
          ...segment,
          video_start_s: 10,
          words: [
            { word_index: 4501, text: "She", t_start: 10.1, t_end: 10.32 },
            { word_index: 4502, text: "stood", t_start: 10.32, t_end: 10.61 },
          ],
        },
      ],
    };
    // Local (default) double-counts: 10 + 10.32 = 20.32.
    expect(seekTargetForWord(stitched, 4502)?.videoTimeS).toBeCloseTo(20.32);
    // Absolute uses the word time directly.
    expect(seekTargetForWord(stitched, 4502, true)?.videoTimeS).toBeCloseTo(10.32);
  });
});

describe("shotIndexForWord tolerates a shot with no source_span", () => {
  it("treats a spanless shot as sorting after real words rather than throwing", () => {
    const spanned: Shot[] = [
      { shot_id: "s1", beat_id: "b1", scene_id: "sc1", status: "accepted", source_span: { page: 1, word_range: [0, 30] } },
      { shot_id: "s2", beat_id: "b2", scene_id: "sc1", status: "accepted", source_span: null },
    ];
    expect(() => shotIndexForWord(spanned, 10)).not.toThrow();
    expect(shotIndexForWord(spanned, 10)).toBe(0);
  });
});

describe("source-span index (shotForWord)", () => {
  const shots: Shot[] = [
    { shot_id: "s1", beat_id: "b1", scene_id: "sc1", status: "accepted", source_span: { page: 1, word_range: [0, 30] } },
    { shot_id: "s2", beat_id: "b2", scene_id: "sc1", status: "accepted", source_span: { page: 1, word_range: [30, 70] } },
    { shot_id: "s3", beat_id: "b3", scene_id: "sc2", status: "accepted", source_span: { page: 2, word_range: [70, 120] } },
  ];

  it("binary-searches the shot covering a word", () => {
    expect(shotIndexForWord(shots, 0)).toBe(0);
    expect(shotForWord(shots, 45)?.shot_id).toBe("s2");
    expect(shotForWord(shots, 119)?.shot_id).toBe("s3");
  });

  it("returns null before the first shot", () => {
    const later: Shot[] = [{ ...shots[0], source_span: { page: 1, word_range: [10, 30] } }];
    expect(shotIndexForWord(later, 5)).toBeNull();
  });
});
