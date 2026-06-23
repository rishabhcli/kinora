import { describe, expect, it } from "vitest";

import type { ShotResponse } from "./api/types";
import {
  contentNormFromPixels,
  contentNormToElementRect,
  sceneWindow,
  toDirectorShots,
} from "./director";

const shots: ShotResponse[] = [
  {
    shot_id: "s1",
    scene_id: "sc1",
    beat_id: "b1",
    status: "accepted",
    source_span: { page: 0, word_range: [0, 49] },
    duration_s: 5,
    clip_url: "c1",
    qa: { ccs: 0.92, verdict: "pass" },
    reference_image_ids: [],
  },
  {
    shot_id: "s2",
    scene_id: "sc1",
    beat_id: "b2",
    status: "planned",
    source_span: { page: 1, word_range: [50, 99] },
    duration_s: 6,
    clip_url: null,
    qa: null,
    reference_image_ids: [],
  },
  {
    shot_id: "s3",
    scene_id: "sc2",
    beat_id: "b3",
    status: "planned",
    source_span: { page: 2, word_range: [100, 149] },
    duration_s: 4,
    clip_url: null,
    qa: null,
    reference_image_ids: [],
  },
];

describe("toDirectorShots", () => {
  it("projects span + QA and numbers shots within each scene", () => {
    const tiles = toDirectorShots(shots);
    expect(tiles[0]).toMatchObject({
      shotId: "s1",
      sceneIndex: 1,
      startWord: 0,
      endWord: 49,
      page: 0,
      durationS: 5,
      clipUrl: "c1",
      status: "ready",
    });
    expect(tiles[0]?.qa).toEqual({ ccs: 0.92, score: null, passed: true });
    // Same scene -> next index; no clip -> pending.
    expect(tiles[1]).toMatchObject({ sceneIndex: 2, status: "pending" });
    // New scene -> index resets.
    expect(tiles[2]).toMatchObject({ sceneId: "sc2", sceneIndex: 1 });
  });

  it("layers live updates: optimistic regenerating, then the swapped clip + QA", () => {
    expect(toDirectorShots(shots, { s2: { status: "regenerating" } })[1]?.status).toBe(
      "regenerating",
    );
    const swapped = toDirectorShots(shots, {
      s2: { clipUrl: "c2v2", qa: { ccs: 0.7, verdict: "fail" }, status: "ready" },
    })[1];
    expect(swapped).toMatchObject({ clipUrl: "c2v2", status: "ready" });
    expect(swapped?.qa).toEqual({ ccs: 0.7, score: null, passed: false });
  });
});

describe("sceneWindow", () => {
  it("windows to the current shot's scene, else the whole book", () => {
    const tiles = toDirectorShots(shots);
    expect(sceneWindow(tiles, "s1").map((s) => s.shotId)).toEqual(["s1", "s2"]);
    expect(sceneWindow(tiles, "s3").map((s) => s.shotId)).toEqual(["s3"]);
    expect(sceneWindow(tiles, null)).toHaveLength(3);
    expect(sceneWindow(tiles, "missing")).toHaveLength(3);
  });
});

describe("contentNormFromPixels", () => {
  it("maps proportionally when the content fills the element (no letterbox)", () => {
    // 1920x1080 video shown in a 1600x900 box (both 16:9) -> content fills it.
    const box = contentNormFromPixels(1600, 900, 1920, 1080, { x: 800, y: 0, w: 800, h: 450 });
    expect(box).toEqual({ x: 0.5, y: 0, w: 0.5, h: 0.5 });
  });

  it("subtracts the pillarbox bars for a non-matching aspect", () => {
    // 1080x1080 (1:1) video in a 1600x900 (16:9) box -> 900px content, 350px bars.
    const box = contentNormFromPixels(1600, 900, 1080, 1080, { x: 350, y: 0, w: 450, h: 450 });
    expect(box?.x).toBeCloseTo(0, 5);
    expect(box?.w).toBeCloseTo(0.5, 5);
    expect(box?.h).toBeCloseTo(0.5, 5);
  });

  it("clamps a box that spills past the content edges", () => {
    const box = contentNormFromPixels(1000, 1000, 1000, 1000, { x: -200, y: -200, w: 600, h: 600 });
    expect(box).toEqual({ x: 0, y: 0, w: 0.4, h: 0.4 });
  });

  it("returns null with no intrinsic size or a hairline drag", () => {
    expect(contentNormFromPixels(1000, 1000, 0, 0, { x: 0, y: 0, w: 100, h: 100 })).toBeNull();
    expect(contentNormFromPixels(1000, 1000, 1000, 1000, { x: 0, y: 0, w: 5, h: 5 })).toBeNull();
  });

  it("round-trips through contentNormToElementRect (with the pillarbox offset)", () => {
    // 1:1 video in a 16:9 box -> 350px pillarbox bars. A content box at x=0,w=0.5
    // maps back to 350px (21.875%) from the left, 450px (28.125%) wide.
    const rect = contentNormToElementRect(1600, 900, 1080, 1080, { x: 0, y: 0, w: 0.5, h: 0.5 });
    expect(rect?.leftPct).toBeCloseTo(21.875, 3);
    expect(rect?.widthPct).toBeCloseTo(28.125, 3);
    expect(rect?.topPct).toBeCloseTo(0, 3);
    expect(rect?.heightPct).toBeCloseTo(50, 3);

    // A 16:9 clip filling a 16:9 box is a plain percentage map (no bars).
    const fill = contentNormToElementRect(1600, 900, 1920, 1080, { x: 0.5, y: 0, w: 0.5, h: 0.5 });
    expect(fill).toMatchObject({ leftPct: 50, topPct: 0, widthPct: 50, heightPct: 50 });
  });

  it("returns null with no intrinsic size for the inverse map too", () => {
    expect(contentNormToElementRect(1600, 900, 0, 0, { x: 0, y: 0, w: 1, h: 1 })).toBeNull();
  });
});
