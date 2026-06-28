import { describe, it, expect } from "vitest";
import {
  sortShotsByReadingOrder,
  buildSceneLanes,
  isShotRenderable,
  shotReferencesEntity,
  canonEditBlastRadius,
  type DirectorShot,
} from "./director";

function shot(over: Partial<DirectorShot> = {}): DirectorShot {
  return {
    shot_id: "s1",
    beat_id: "b1",
    scene_id: "sc1",
    source_span: null,
    status: "accepted",
    render_mode: "reference_to_video",
    duration_s: 5,
    qa: null,
    clip_url: "http://minio:9000/kinora/clip.mp4",
    reference_image_ids: [],
    ...over,
  };
}

describe("sortShotsByReadingOrder", () => {
  it("orders by word_range start, ascending", () => {
    const shots = [
      shot({ shot_id: "c", source_span: { word_range: [300, 350] } }),
      shot({ shot_id: "a", source_span: { word_range: [0, 50] } }),
      shot({ shot_id: "b", source_span: { word_range: [100, 150] } }),
    ];
    expect(sortShotsByReadingOrder(shots).map((s) => s.shot_id)).toEqual(["a", "b", "c"]);
  });

  it("places shots with no word range after those that have one", () => {
    const shots = [
      shot({ shot_id: "none", source_span: null }),
      shot({ shot_id: "first", source_span: { word_range: [10, 20] } }),
    ];
    expect(sortShotsByReadingOrder(shots).map((s) => s.shot_id)).toEqual(["first", "none"]);
  });

  it("breaks ties by beat then shot id, and does not mutate the input", () => {
    const input = [
      shot({ shot_id: "z", beat_id: "b2", source_span: { word_range: [0, 5] } }),
      shot({ shot_id: "a", beat_id: "b1", source_span: { word_range: [0, 5] } }),
    ];
    const out = sortShotsByReadingOrder(input);
    expect(out.map((s) => s.shot_id)).toEqual(["a", "z"]);
    expect(input.map((s) => s.shot_id)).toEqual(["z", "a"]); // input untouched
  });
});

describe("buildSceneLanes", () => {
  it("groups shots into per-scene lanes ordered by reading position", () => {
    const shots = [
      shot({ shot_id: "s2", scene_id: "B", source_span: { word_range: [200, 250] }, duration_s: 4 }),
      shot({ shot_id: "s1", scene_id: "A", source_span: { word_range: [0, 50] }, duration_s: 6 }),
      shot({ shot_id: "s3", scene_id: "A", source_span: { word_range: [60, 90] }, duration_s: 3 }),
    ];
    const lanes = buildSceneLanes(shots);
    expect(lanes.map((l) => l.scene_id)).toEqual(["A", "B"]);
    expect(lanes[0].shots.map((s) => s.shot_id)).toEqual(["s1", "s3"]);
    expect(lanes[0].duration_s).toBe(9);
    expect(lanes[0].word_start).toBe(0);
    expect(lanes[0].word_end).toBe(90);
  });

  it("buckets null scene_id under (unscened) and never drops shots", () => {
    const shots = [shot({ shot_id: "x", scene_id: null, source_span: null })];
    const lanes = buildSceneLanes(shots);
    expect(lanes).toHaveLength(1);
    expect(lanes[0].scene_id).toBe("(unscened)");
    expect(lanes[0].shots).toHaveLength(1);
  });
});

describe("isShotRenderable", () => {
  it("is true only with a clip and a terminal status", () => {
    expect(isShotRenderable(shot({ status: "accepted", clip_url: "u" }))).toBe(true);
    expect(isShotRenderable(shot({ status: "PROMOTED", clip_url: "u" }))).toBe(true);
    expect(isShotRenderable(shot({ status: "planned", clip_url: "u" }))).toBe(false);
    expect(isShotRenderable(shot({ status: "accepted", clip_url: null }))).toBe(false);
  });
});

describe("canon dependency helpers", () => {
  it("matches an entity reference with or without a version suffix", () => {
    const s = shot({ reference_image_ids: ["hero@3", "ship"] });
    expect(shotReferencesEntity(s, "hero")).toBe(true);
    expect(shotReferencesEntity(s, "ship")).toBe(true);
    expect(shotReferencesEntity(s, "villain")).toBe(false);
  });

  it("counts the blast radius of a canon edit", () => {
    const shots = [
      shot({ shot_id: "a", reference_image_ids: ["hero@1"] }),
      shot({ shot_id: "b", reference_image_ids: ["ship"] }),
      shot({ shot_id: "c", reference_image_ids: ["hero@2", "ship"] }),
    ];
    expect(canonEditBlastRadius(shots, "hero")).toBe(2);
    expect(canonEditBlastRadius(shots, "ship")).toBe(2);
    expect(canonEditBlastRadius(shots, "nobody")).toBe(0);
  });
});
