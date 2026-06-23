import { describe, expect, it, vi } from "vitest";

import type { ShotResponse } from "../api/types";
import type { SyncSegment } from "../events";
import { SyncEngine } from "./SyncEngine";
import {
  buildStitchedScene,
  buildTimeline,
  sceneTimeForWord,
  segmentIndexAtTime,
  segmentIndexForWord,
} from "./timeline";

// Two scenes: sc1 = shots s1+s2 (words 0..99, page 1), sc2 = shot s3 (words 100..149, page 2).
const shots: ShotResponse[] = [
  {
    shot_id: "s1",
    scene_id: "sc1",
    status: "accepted",
    source_span: { page: 1, word_range: [0, 49] },
    duration_s: 5,
    clip_url: "clipS1",
  },
  {
    shot_id: "s2",
    scene_id: "sc1",
    status: "accepted",
    source_span: { page: 1, word_range: [50, 99] },
    duration_s: 5,
    clip_url: "clipS2",
  },
  {
    shot_id: "s3",
    scene_id: "sc2",
    status: "accepted",
    source_span: { page: 2, word_range: [100, 149] },
    duration_s: 6,
    clip_url: "clipS3",
  },
];

// sc1 sync map, already in absolute scene time (the backend's merge_sync_segments).
const sc1Segments: SyncSegment[] = [
  {
    shot_id: "s1",
    video_start_s: 0,
    video_end_s: 5,
    page: 1,
    page_turn_at_s: 4.8,
    words: [
      { word_index: 0, text: "a", t_start: 0, t_end: 1, bbox: null },
      { word_index: 25, text: "b", t_start: 2.5, t_end: 3, bbox: null },
    ],
  },
  {
    shot_id: "s2",
    video_start_s: 5,
    video_end_s: 10,
    page: 1,
    page_turn_at_s: 9.8,
    words: [
      { word_index: 50, text: "c", t_start: 5, t_end: 6, bbox: null },
      { word_index: 75, text: "d", t_start: 7.5, t_end: 8, bbox: null },
    ],
  },
];

const sc2Segments: SyncSegment[] = [
  {
    shot_id: "s3",
    video_start_s: 0,
    video_end_s: 6,
    page: 2,
    page_turn_at_s: 5.8,
    words: [{ word_index: 100, text: "e", t_start: 0, t_end: 1, bbox: null }],
  },
];

describe("buildStitchedScene + scene lookups (timeline)", () => {
  const timeline = buildTimeline(shots);

  it("builds a scene with absolute-time segments + a word range from the shots", () => {
    const scene = buildStitchedScene("sc1", "sceneSc1.mp4", sc1Segments, timeline);
    expect(scene).not.toBeNull();
    expect(scene?.clipUrl).toBe("sceneSc1.mp4");
    expect(scene?.startWord).toBe(0);
    expect(scene?.endWord).toBe(99);
    expect(scene?.durationS).toBe(10);
    expect(scene?.segments.map((s) => s.shot_id)).toEqual(["s1", "s2"]);
    // Each segment is annotated with its shot's source word range.
    expect(scene?.segments[1]).toMatchObject({ startWord: 50, endWord: 99 });
  });

  it("returns null with no clip or no segments", () => {
    expect(buildStitchedScene("sc1", null, sc1Segments, timeline)).toBeNull();
    expect(buildStitchedScene("sc1", "x.mp4", [], timeline)).toBeNull();
  });

  it("segmentIndexAtTime finds the segment covering an absolute time", () => {
    const scene = buildStitchedScene("sc1", "x.mp4", sc1Segments, timeline)!;
    expect(segmentIndexAtTime(scene, 3)).toBe(0);
    expect(segmentIndexAtTime(scene, 6)).toBe(1);
    expect(segmentIndexAtTime(scene, -1)).toBe(-1);
    expect(segmentIndexAtTime(scene, 50)).toBe(1); // past the end clamps to the last
  });

  it("segmentIndexForWord finds the segment whose shot covers a word", () => {
    const scene = buildStitchedScene("sc1", "x.mp4", sc1Segments, timeline)!;
    expect(segmentIndexForWord(scene, 25)).toBe(0);
    expect(segmentIndexForWord(scene, 75)).toBe(1);
    expect(segmentIndexForWord(scene, -5)).toBe(-1);
  });

  it("sceneTimeForWord maps a global word to absolute time in the stitched asset", () => {
    const scene = buildStitchedScene("sc1", "x.mp4", sc1Segments, timeline)!;
    expect(sceneTimeForWord(scene, 0)).toBe(0);
    expect(sceneTimeForWord(scene, 25)).toBe(2.5);
    expect(sceneTimeForWord(scene, 50)).toBe(5);
    expect(sceneTimeForWord(scene, 75)).toBe(7.5);
    // A word between narrated words lands on the latest one at/before it.
    expect(sceneTimeForWord(scene, 10)).toBe(0); // in s1, after word 0, before 25
    expect(sceneTimeForWord(scene, 60)).toBe(5); // in s2, after word 50, before 75
  });
});

describe("SyncEngine scene-level playback (§9.6)", () => {
  it("prefers the stitched scene over per-shot clips for its word range", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.reportScroll(10, 0);
    expect(engine.getSnapshot().currentSource).toMatchObject({ kind: "shot", id: "s1" });
    expect(engine.getSnapshot().currentClipUrl).toBe("clipS1");

    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments);
    const snap = engine.getSnapshot();
    expect(snap.currentSource).toMatchObject({ kind: "scene", id: "sc1", url: "sceneSc1.mp4" });
    expect(snap.currentClipUrl).toBe("sceneSc1.mp4");
    expect(snap.currentStage).toBe("full_video");
  });

  it("scrolling across shots within a scene keeps one continuous source (gapless)", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments); // focus word 0 ∈ sc1
    expect(engine.getSnapshot().currentClipUrl).toBe("sceneSc1.mp4");

    engine.reportScroll(60, 0); // still inside sc1, now over shot s2
    const snap = engine.getSnapshot();
    expect(snap.currentClipUrl).toBe("sceneSc1.mp4"); // URL unchanged → no reload/flash
    expect(snap.currentShotId).toBe("s2"); // but the shot under the playhead advances
  });

  it("maps absolute scene time to the karaoke highlight + shot (video owns)", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments);
    engine.reportScroll(10, 0); // scroll owns until t=1200

    engine.reportVideoTime(7.6, 5000); // absolute scene time, grace expired
    const snap = engine.getSnapshot();
    expect(snap.owner).toBe("video");
    expect(snap.highlightWordIndex).toBe(75); // word d at t≥7.5 in segment s2
    expect(snap.focusWord).toBe(75);
    expect(snap.currentShotId).toBe("s2");
  });

  it("hot-swaps from per-shot playback to the stitched scene, seeking to the current word", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.reportScroll(60, 0); // on shot s2 clip
    expect(engine.getSnapshot().currentSource).toMatchObject({ kind: "shot", id: "s2" });
    const seqBefore = engine.getSnapshot().playheadSeekSeq;

    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments);
    const snap = engine.getSnapshot();
    expect(snap.currentSource).toMatchObject({ kind: "scene", id: "sc1" });
    // Continue from word 60's frame in the scene (≈ word 50's t_start = 5s), not 0.
    expect(snap.playheadSeekS).toBe(5);
    expect(snap.playheadSeekSeq).toBeGreaterThan(seqBefore);
  });

  it("seek to a mid-scene word lands on the word's frame + highlight (§4.8)", () => {
    const onSeek = vi.fn();
    const engine = new SyncEngine({ callbacks: { onSeek } });
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments);

    engine.seek(75, 0);
    const snap = engine.getSnapshot();
    expect(onSeek).toHaveBeenCalledWith(75);
    expect(snap.focusWord).toBe(75);
    expect(snap.currentSource).toMatchObject({ kind: "scene", id: "sc1" });
    expect(snap.currentShotId).toBe("s2");
    expect(snap.playheadSeekS).toBe(7.5); // word 75's absolute time in the stitched asset
  });

  it("falls back to the per-shot clip for a scene that is not yet stitched", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments); // only sc1 stitched
    engine.reportScroll(120, 0); // word 120 ∈ sc2 (unstitched)

    const snap = engine.getSnapshot();
    expect(snap.currentSource).toMatchObject({ kind: "shot", id: "s3", url: "clipS3" });
    expect(snap.currentClipUrl).toBe("clipS3");
  });

  it("exposes nextSource for the hidden preload buffer, preferring a stitched next scene", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments); // on sc1, word 0
    // Next is sc2's first shot clip until sc2 is stitched.
    expect(engine.getSnapshot().nextSource).toMatchObject({ kind: "shot", id: "s3" });

    engine.ingestScene("sc2", "sceneSc2.mp4", sc2Segments);
    // Now the upcoming boundary preloads the stitched next scene instead.
    expect(engine.getSnapshot().nextSource).toMatchObject({ kind: "scene", id: "sc2" });
  });

  it("adopts the source the shell reports at a boundary (sourceId), then reads its time base", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments);
    engine.ingestScene("sc2", "sceneSc2.mp4", sc2Segments);
    engine.reportScroll(10, 0); // on sc1

    // The shell crossed the boundary and is now playing sc2 (its preloaded buffer).
    engine.reportVideoTime(0.5, 5000, "sc2");
    const snap = engine.getSnapshot();
    expect(snap.currentSource).toMatchObject({ kind: "scene", id: "sc2" });
    expect(snap.currentClipUrl).toBe("sceneSc2.mp4");
    expect(snap.focusWord).toBe(100); // word e in sc2 at t≈0.5
    expect(snap.currentShotId).toBe("s3");
  });

  it("advanceToNextSource flows continuously into the next scene when the asset ends", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments);
    engine.ingestScene("sc2", "sceneSc2.mp4", sc2Segments);
    engine.reportScroll(10, 0); // on sc1
    expect(engine.getSnapshot().nextSource).toMatchObject({ kind: "scene", id: "sc2" });

    const advanced = engine.advanceToNextSource();
    expect(advanced).toBe(true);
    const snap = engine.getSnapshot();
    expect(snap.currentSource).toMatchObject({ kind: "scene", id: "sc2" });
    expect(snap.focusWord).toBe(100); // enters sc2 at its head
    expect(snap.currentPage).toBe(2);
  });

  it("advanceToNextSource is a no-op (false) at the end of the book", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.ingestScene("sc2", "sceneSc2.mp4", sc2Segments);
    engine.reportScroll(120, 0); // on the last content, nothing queued after
    expect(engine.getSnapshot().nextSource).toBeNull();
    expect(engine.advanceToNextSource()).toBe(false);
  });
});

describe("SyncEngine scene robustness", () => {
  // A second book whose shots/scenes don't overlap the first.
  const otherShots: ShotResponse[] = [
    {
      shot_id: "z1",
      scene_id: "scz",
      status: "accepted",
      source_span: { page: 1, word_range: [0, 40] },
      duration_s: 4,
      clip_url: "clipZ1",
    },
  ];

  it("prunes a previous book's stitched scenes when a new shot list loads", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments);
    engine.reportScroll(10, 0);
    expect(engine.getSnapshot().currentSource).toMatchObject({ kind: "scene", id: "sc1" });

    // Switch books — the old scene must not leak into the new one's playhead.
    engine.setShots(otherShots);
    engine.reportScroll(10, 100);
    const snap = engine.getSnapshot();
    expect(snap.currentSource).toMatchObject({ kind: "shot", id: "z1", url: "clipZ1" });
    // sc1 was pruned: word 60 (inside the old scene) no longer resolves to a
    // scene source in the new book.
    engine.reportScroll(60, 200);
    expect(engine.getSnapshot().currentSource?.kind).not.toBe("scene");
  });

  it("keeps live scenes when the shot list merely grows during ingest", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments);
    engine.reportScroll(10, 0);
    // A re-fetch that returns the same book (a superset) must not drop sc1.
    engine.setShots(shots);
    expect(engine.getSnapshot().currentSource).toMatchObject({ kind: "scene", id: "sc1" });
  });

  it("evicts a stale stitched scene when one of its shots is regenerated", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments);
    engine.reportScroll(10, 0);
    expect(engine.getSnapshot().currentClipUrl).toBe("sceneSc1.mp4");

    // A regen of shot s1 makes the concatenated mp4 stale → fall back to the fresh clip.
    engine.swapClipUrl("s1", "clipS1-v2");
    const snap = engine.getSnapshot();
    expect(snap.currentSource).toMatchObject({ kind: "shot", id: "s1", url: "clipS1-v2" });
  });

  it("re-stitch supersedes the stale scene in place, preserving the playhead", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments);
    engine.reportScroll(60, 0); // on shot s2 within sc1
    const seqBefore = engine.getSnapshot().playheadSeekSeq;

    // A fresh stitch for the same scene (new mp4) while we're watching it.
    engine.ingestScene("sc1", "sceneSc1-v2.mp4", sc1Segments);
    const snap = engine.getSnapshot();
    expect(snap.currentClipUrl).toBe("sceneSc1-v2.mp4");
    expect(snap.playheadSeekS).toBe(5); // continue from word 60 (≈ seg s2 head, 5s)
    expect(snap.playheadSeekSeq).toBeGreaterThan(seqBefore);
  });

  it("markSourceFailed drops a dead scene URL and falls back to the per-shot clip", () => {
    const onSourceError = vi.fn();
    const engine = new SyncEngine({ callbacks: { onSourceError } });
    engine.setShots(shots);
    engine.ingestScene("sc1", "sceneSc1.mp4", sc1Segments);
    engine.reportScroll(10, 0);
    expect(engine.getSnapshot().currentSource).toMatchObject({ kind: "scene", id: "sc1" });

    engine.markSourceFailed("sc1"); // e.g. the presigned URL expired
    const snap = engine.getSnapshot();
    expect(snap.currentSource).toMatchObject({ kind: "shot", id: "s1", url: "clipS1" });
    expect(onSourceError).toHaveBeenCalledWith("sc1");
  });

  it("markSourceFailed on the last available rung degrades to the bridge (no source)", () => {
    const engine = new SyncEngine();
    engine.setShots(shots);
    engine.reportScroll(10, 0);
    expect(engine.getSnapshot().currentSource).toMatchObject({ kind: "shot", id: "s1" });
    engine.markSourceFailed("s1"); // the shot clip URL is dead too
    expect(engine.getSnapshot().currentSource).toBeNull(); // → §12.4 bridge takes over
  });

  it("does not dead-stop at a starved boundary: advances onto the next beat for the bridge", () => {
    const engine = new SyncEngine();
    engine.setShots(shots); // s1,s2,s3 — but no clips ingested for s2/s3 beyond /shots
    // Drop s2's clip so the boundary past s1 has no committed next source.
    engine.markSourceFailed("s2");
    engine.reportScroll(10, 0); // on s1
    expect(engine.getSnapshot().nextSource).toBeNull(); // s2 has no clip to preload

    const advanced = engine.advanceToNextSource();
    expect(advanced).toBe(true); // advanced onto s2's beat for the bridge, no stop
    expect(engine.getSnapshot().focusWord).toBe(50); // s2's first word
    expect(engine.getSnapshot().currentSource).toBeNull(); // bridge rung, not a clip
  });
});
