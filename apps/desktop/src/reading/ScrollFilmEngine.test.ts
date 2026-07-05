// timelineFromProps (ScrollFilmEngine's live-shots → Timeline adapter) is a
// plain, DOM-free function, but it lives inside ScrollFilmEngine.tsx — a .tsx
// module that also exports a React component. Node's `--experimental-strip-types`
// (the plain-node harness `reading/__tests__/timeline.test.ts` uses for
// timeline.ts's pure core) cannot load a `.tsx` file at all ("Unknown file
// extension .tsx"), so — mirroring the same rule already documented in
// vitest.config.ts for webglCompositor.test.ts ("tests that import sibling
// *source* modules ... stay on vitest") — this test runs under vitest/jsdom
// instead. It is co-located next to ScrollFilmEngine.tsx (not under
// reading/__tests__/) specifically so vitest.config.ts's blanket
// `"src/reading/__tests__/**/*.test.ts"` exclude doesn't swallow it, and so
// `run-node-tests.mjs`'s content-sniffing walker (which only picks up files
// referencing Node's built-in test runner or the reading/__tests__ harness by
// name) skips it.
import { describe, expect, it } from "vitest";
import { timelineFromProps } from "./ScrollFilmEngine";
import { ClipCache } from "./clipCache";
import type { ShotResponse } from "../lib/api";

// No real network in tests: ClipCache.resolve() only returns a cached blob URL
// once a background fetch completes, and a real (uncached) call is expected to
// pass its input straight through synchronously — but it also kicks off a
// best-effort background fetch. Stub fetch so that background fetch can't hang
// on/attempt real DNS in CI.
const noNetworkFetch = (() => Promise.reject(new Error("no network in tests"))) as typeof fetch;

function shot(over: Partial<ShotResponse> & Pick<ShotResponse, "shot_id" | "clip_url">): ShotResponse {
  return {
    status: "ready",
    duration_s: 5,
    source_span: { word_range: [0, 10] },
    clip_start_s: null,
    clip_end_s: null,
    ...over,
  };
}

function timeline(shots: ShotResponse[]) {
  return timelineFromProps(shots, {}, true, "fallback.mp4", new ClipCache(12, noNetworkFetch));
}

describe("timelineFromProps merged-clip grouping", () => {
  it("gives each shot its own src when clip_start_s/clip_end_s are absent (today's unchanged behavior)", () => {
    const shots = [
      shot({ shot_id: "s1", clip_url: "http://x/s1.mp4", source_span: { word_range: [0, 10] } }),
      shot({ shot_id: "s2", clip_url: "http://x/s2.mp4", source_span: { word_range: [10, 20] } }),
    ];
    const tl = timeline(shots);
    expect(tl.segments[0].src).not.toBe(tl.segments[1].src);
    // Whole-clip window derived from duration_s, exactly as before this change.
    expect(tl.segments[0].clipStart).toBe(0);
    expect(tl.segments[0].clipEnd).toBe(5);
  });

  it("groups shots sharing a clip_url into one src with real clipStart/clipEnd offsets", () => {
    const shots = [
      shot({
        shot_id: "s1",
        clip_url: "http://x/event1.mp4",
        duration_s: 15,
        clip_start_s: 0,
        clip_end_s: 5,
        source_span: { word_range: [0, 10] },
      }),
      shot({
        shot_id: "s2",
        clip_url: "http://x/event1.mp4",
        duration_s: 15,
        clip_start_s: 5,
        clip_end_s: 10,
        source_span: { word_range: [10, 20] },
      }),
      shot({
        shot_id: "s3",
        clip_url: "http://x/event1.mp4",
        duration_s: 15,
        clip_start_s: 10,
        clip_end_s: 15,
        source_span: { word_range: [20, 30] },
      }),
    ];
    const tl = timeline(shots);
    expect(new Set(tl.segments.map((s) => s.src)).size).toBe(1);
    expect(tl.segments[0].clipStart).toBe(0);
    expect(tl.segments[0].clipEnd).toBe(5);
    expect(tl.segments[1].clipStart).toBe(5);
    expect(tl.segments[1].clipEnd).toBe(10);
    expect(tl.segments[2].clipStart).toBe(10);
    expect(tl.segments[2].clipEnd).toBe(15);
  });

  it("falls back to duration_s for clipEnd when only clip_start_s is present", () => {
    const shots = [
      shot({
        shot_id: "s1",
        clip_url: "http://x/event1.mp4",
        duration_s: 12,
        clip_start_s: 4,
        clip_end_s: null,
        source_span: { word_range: [0, 10] },
      }),
    ];
    const tl = timeline(shots);
    expect(tl.segments[0].clipStart).toBe(4);
    expect(tl.segments[0].clipEnd).toBe(12);
  });
});
