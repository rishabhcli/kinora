// Pure decode-health deltas from cumulative playback-quality counters — node:test.
import test from "node:test";
import assert from "node:assert/strict";
import { DecodeStats, classifyDecode } from "./decodeStats.ts";

test("first reading yields no delta (no baseline)", () => {
  const ds = new DecodeStats();
  assert.equal(ds.push({ totalVideoFrames: 100, droppedVideoFrames: 0, atMs: 0 }), null);
  assert.equal(ds.health(), "good");
});

test("a clean interval is good with a sensible presented-fps", () => {
  const ds = new DecodeStats();
  ds.push({ totalVideoFrames: 0, droppedVideoFrames: 0, atMs: 0 });
  const d = ds.push({ totalVideoFrames: 30, droppedVideoFrames: 0, atMs: 1000 });
  assert.ok(d);
  assert.equal(d!.presented, 30);
  assert.equal(d!.dropped, 0);
  assert.equal(d!.dropRate, 0);
  assert.ok(Math.abs(d!.presentedFps - 30) < 0.01);
  assert.equal(ds.health(), "good");
});

test("a high drop rate is graded degraded then stalled", () => {
  const degraded = new DecodeStats();
  degraded.push({ totalVideoFrames: 0, droppedVideoFrames: 0, atMs: 0 });
  // 1 dropped of 20 total → 5% drop → degraded threshold.
  const d1 = degraded.push({ totalVideoFrames: 20, droppedVideoFrames: 1, atMs: 1000 });
  assert.equal(classifyDecode(d1), "degraded");

  const stalled = new DecodeStats();
  stalled.push({ totalVideoFrames: 0, droppedVideoFrames: 0, atMs: 0 });
  // 5 dropped of 20 → 25% → stalled.
  const d2 = stalled.push({ totalVideoFrames: 20, droppedVideoFrames: 5, atMs: 1000 });
  assert.equal(classifyDecode(d2), "stalled");
});

test("a counter reset (source swap) re-baselines without a negative delta", () => {
  const ds = new DecodeStats();
  ds.push({ totalVideoFrames: 500, droppedVideoFrames: 10, atMs: 0 });
  // New <video> element: counters restart lower → treated as a reset, no delta.
  const d = ds.push({ totalVideoFrames: 5, droppedVideoFrames: 0, atMs: 1000 });
  assert.equal(d, null);
  // The next reading diffs against the new baseline normally.
  const d2 = ds.push({ totalVideoFrames: 35, droppedVideoFrames: 0, atMs: 2000 });
  assert.ok(d2);
  assert.equal(d2!.presented, 30);
});

test("zero presented but frames dropped over a real interval is a stall", () => {
  assert.equal(
    classifyDecode({ presented: 0, dropped: 4, corrupted: 0, elapsedMs: 500, dropRate: 1, presentedFps: 0 }),
    "stalled",
  );
});

test("classifyDecode on null delta defaults good", () => {
  assert.equal(classifyDecode(null), "good");
});
