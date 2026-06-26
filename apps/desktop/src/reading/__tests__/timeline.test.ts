// Run: node --experimental-strip-types apps/desktop/src/reading/__tests__/timeline.test.ts
import { test, eq, ok, close, done } from "./tiny-test.mjs";
import {
  buildTimeline,
  resolvePlayhead,
  focusWordFromFraction,
  segmentTime,
  classifyScroll,
  schedulerSignal,
  nextSegmentToPreload,
  computeFrame,
  scrollVelocity,
  type SegmentInput,
} from "../timeline.ts";

// ---- buildTimeline ------------------------------------------------------- //

test("buildTimeline orders segments by word start and makes them contiguous", () => {
  const inputs: SegmentInput[] = [
    { id: "b", wordStart: 120, wordEnd: 200, src: "b.mp4", duration: 5 },
    { id: "a", wordStart: 0, wordEnd: 100, src: "a.mp4", duration: 4 },
  ];
  const tl = buildTimeline(inputs);
  eq(
    tl.segments.map((s) => [s.id, s.wordStart, s.wordEnd]),
    [
      ["a", 0, 120], // a's gap up to b's start is absorbed → no dead zone while scrubbing
      ["b", 120, 200],
    ],
  );
  eq(tl.totalWords, 200);
});

test("buildTimeline derives clipEnd from duration when not given", () => {
  const tl = buildTimeline([{ id: "a", wordStart: 0, wordEnd: 10, src: "a.mp4", duration: 6 }]);
  eq(tl.segments[0].clipStart, 0);
  eq(tl.segments[0].clipEnd, 6);
});

test("buildTimeline keeps explicit clipStart/clipEnd for stitched event films", () => {
  // Two shots inside one stitched event mp4 (same src, increasing offsets).
  const tl = buildTimeline([
    { id: "s1", wordStart: 0, wordEnd: 50, src: "event-1.mp4", clipStart: 0, clipEnd: 4 },
    { id: "s2", wordStart: 50, wordEnd: 90, src: "event-1.mp4", clipStart: 4, clipEnd: 9 },
  ]);
  eq(tl.segments[1].src, "event-1.mp4");
  eq(tl.segments[1].clipStart, 4);
  eq(tl.segments[1].clipEnd, 9);
});

test("buildTimeline on empty input is an empty, zero-word timeline", () => {
  const tl = buildTimeline([]);
  eq(tl.segments, []);
  eq(tl.totalWords, 0);
});

test("buildTimeline single fallback film spans all words with unknown duration", () => {
  const tl = buildTimeline([{ id: "fallback", wordStart: 0, wordEnd: 1, src: "film.mp4" }]);
  eq(tl.totalWords, 1);
  eq(tl.segments[0].clipStart, 0);
  eq(tl.segments[0].clipEnd, 0); // unknown — runtime uses the live <video> duration
});

// ---- resolvePlayhead ----------------------------------------------------- //

test("resolvePlayhead picks the greatest segment whose start <= focus word", () => {
  const tl = buildTimeline([
    { id: "a", wordStart: 0, wordEnd: 100, src: "a.mp4", duration: 4 },
    { id: "b", wordStart: 100, wordEnd: 200, src: "b.mp4", duration: 4 },
  ]);
  eq(resolvePlayhead(tl, 150)!.segment.id, "b");
  eq(resolvePlayhead(tl, 99)!.segment.id, "a");
  eq(resolvePlayhead(tl, 100)!.segment.id, "b"); // boundary belongs to the later shot
});

test("resolvePlayhead local fraction is the position within the segment word span", () => {
  const tl = buildTimeline([{ id: "a", wordStart: 0, wordEnd: 100, src: "a.mp4", duration: 4 }]);
  close(resolvePlayhead(tl, 0)!.localFraction, 0);
  close(resolvePlayhead(tl, 50)!.localFraction, 0.5);
  close(resolvePlayhead(tl, 100)!.localFraction, 1); // clamps at segment end
});

test("resolvePlayhead clamps a focus word past the end to the last segment", () => {
  const tl = buildTimeline([{ id: "a", wordStart: 0, wordEnd: 100, src: "a.mp4", duration: 4 }]);
  const p = resolvePlayhead(tl, 9999)!;
  eq(p.segment.id, "a");
  close(p.localFraction, 1);
});

test("resolvePlayhead returns null on an empty timeline", () => {
  eq(resolvePlayhead(buildTimeline([]), 5), null);
});

// ---- focusWordFromFraction ---------------------------------------------- //

test("focusWordFromFraction mirrors ReadingRoom's round(frac * totalWords)", () => {
  eq(focusWordFromFraction(0, 1000), 0);
  eq(focusWordFromFraction(0.5, 1000), 500);
  eq(focusWordFromFraction(1, 1000), 1000);
  eq(focusWordFromFraction(1.5, 1000), 1000); // clamps
  eq(focusWordFromFraction(-0.2, 1000), 0); // clamps
});

// ---- segmentTime --------------------------------------------------------- //

test("segmentTime maps local fraction into the clip's [clipStart, clipEnd] window", () => {
  const seg = { id: "s", src: "x", wordStart: 0, wordEnd: 10, clipStart: 4, clipEnd: 9 };
  close(segmentTime(seg, 0), 4);
  close(segmentTime(seg, 1), 9);
  close(segmentTime(seg, 0.5), 6.5);
});

test("segmentTime falls back to the live <video> duration when clip span is unknown", () => {
  const seg = { id: "s", src: "x", wordStart: 0, wordEnd: 10, clipStart: 0, clipEnd: 0 };
  close(segmentTime(seg, 0.5, 12), 6); // 0.5 * 12s
  close(segmentTime(seg, 0.25, 20), 5);
});

// ---- classifyScroll ------------------------------------------------------ //

test("classifyScroll: fast scroll scrubs, slow/at-rest plays forward", () => {
  eq(classifyScroll(0), "play");
  eq(classifyScroll(2), "play"); // gentle reading pace
  eq(classifyScroll(40), "scrub"); // a flick
  eq(classifyScroll(-40), "scrub"); // direction-agnostic
});

// ---- schedulerSignal (reproduces ReadingRoom 197-204) -------------------- //

test("schedulerSignal seeks on a big jump (>120 words)", () => {
  const s = schedulerSignal(0, 500, 0.2);
  eq(s.kind, "seek");
  eq(s.word, 500);
});

test("schedulerSignal posts intent on normal scroll, passing the focus word + velocity", () => {
  const s = schedulerSignal(0, 8, 1); // 8 words in 1s — gentle, in-range velocity
  eq(s.kind, "intent");
  eq(s.word, 8);
  eq(s.velocity, 8);
});

test("schedulerSignal velocity clamps into [2, 12]", () => {
  eq(schedulerSignal(0, 30, 1).velocity, 12); // 30 -> 12
  eq(schedulerSignal(0, 1, 1).velocity, 2); // 1 -> 2
  eq(schedulerSignal(0, 5, 1).velocity, 5); // in range
});

test("schedulerSignal uses default velocity 4 when dt is zero", () => {
  eq(schedulerSignal(0, 5, 0).velocity, 4);
});

// ---- nextSegmentToPreload ------------------------------------------------ //

test("nextSegmentToPreload returns the upcoming segment within lookahead words", () => {
  const tl = buildTimeline([
    { id: "a", wordStart: 0, wordEnd: 100, src: "a.mp4", duration: 4 },
    { id: "b", wordStart: 100, wordEnd: 200, src: "b.mp4", duration: 4 },
  ]);
  eq(nextSegmentToPreload(tl, 95, 20)!.id, "b"); // approaching the boundary
  eq(nextSegmentToPreload(tl, 10, 20), null); // boundary still far away
  eq(nextSegmentToPreload(tl, 150, 20), null); // already in the last segment
});

// ---- computeFrame (the per-rAF-frame glue, kept pure) -------------------- //

const twoShot = buildTimeline([
  { id: "a", wordStart: 0, wordEnd: 100, src: "a.mp4", duration: 4 },
  { id: "b", wordStart: 100, wordEnd: 200, src: "b.mp4", duration: 6 },
]);

test("computeFrame maps scroll position to src + currentTime at rest (play mode)", () => {
  const f = computeFrame({ timeline: twoShot, scrollTop: 0, scrollRange: 1000, velocityWordsPerSec: 0 });
  eq(f.focusWord, 0);
  eq(f.src, "a.mp4");
  close(f.time, 0);
  eq(f.mode, "play");
});

test("computeFrame at mid-scroll picks the right segment and clip time", () => {
  // fraction 0.75 → focusWord 150 → segment b, local (150-100)/100 = 0.5 → time 3s
  const f = computeFrame({ timeline: twoShot, scrollTop: 750, scrollRange: 1000, velocityWordsPerSec: 0 });
  eq(f.focusWord, 150);
  eq(f.src, "b.mp4");
  close(f.time, 3); // clipStart 0 + 0.5 * 6s
});

test("computeFrame enters scrub mode under a fast flick", () => {
  const f = computeFrame({ timeline: twoShot, scrollTop: 500, scrollRange: 1000, velocityWordsPerSec: 80 });
  eq(f.mode, "scrub");
});

test("computeFrame uses live video duration for the unknown-length fallback film", () => {
  const fallback = buildTimeline([{ id: "fallback", wordStart: 0, wordEnd: 1000, src: "film.mp4" }]);
  const f = computeFrame({ timeline: fallback, scrollTop: 250, scrollRange: 1000, velocityWordsPerSec: 0, liveDuration: 40 });
  close(f.time, 10); // fraction 0.25 → 0.25 * 40s
});

test("computeFrame on an empty timeline yields no src and play mode", () => {
  const f = computeFrame({ timeline: buildTimeline([]), scrollTop: 10, scrollRange: 100, velocityWordsPerSec: 0 });
  eq(f.src, "");
  eq(f.segment, null);
  eq(f.mode, "play");
});

test("computeFrame guards a zero scroll range (content not yet scrollable)", () => {
  const f = computeFrame({ timeline: twoShot, scrollTop: 0, scrollRange: 0, velocityWordsPerSec: 0 });
  eq(f.fraction, 0);
  eq(f.focusWord, 0);
});

// ---- scrollVelocity (post-idle jump must still read as fast) ------------- //

test("scrollVelocity clamps a stale dt so a jump after idle still reads fast", () => {
  // 500-word jump reported with a bogus 4s dt (loop had gone idle) → clamp to the
  // 0.05s max → 10000 wps, decisively a flick.
  close(scrollVelocity(0, 500, 4, 0.05), 10000);
});

test("scrollVelocity uses the real dt for warm, continuous scrolling", () => {
  close(scrollVelocity(100, 110, 0.016), 10 / 0.016); // 625 wps
});

test("scrollVelocity is zero when dt is non-positive", () => {
  eq(scrollVelocity(0, 50, 0), 0);
});

await done();
