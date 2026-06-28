// Pure frame-budget / jank / decode-window stats — no DOM, runnable with node:test.
import test from "node:test";
import assert from "node:assert/strict";
import { FrameStats, percentile } from "./frameStats.ts";

const SIXTY = 1000 / 60; // ≈16.67ms

test("empty stats report zeros, not NaN", () => {
  const fs = new FrameStats();
  const s = fs.snapshot();
  assert.equal(s.count, 0);
  assert.equal(s.fps, 0);
  assert.equal(s.meanMs, 0);
  assert.equal(s.jankRatio, 0);
  assert.equal(fs.hasData, false);
});

test("steady 60fps frames read as ~60fps with no jank", () => {
  const fs = new FrameStats();
  for (let i = 0; i < 120; i++) fs.record(SIXTY);
  const s = fs.snapshot();
  assert.equal(s.count, 120);
  assert.ok(Math.abs(s.fps - 60) < 0.5, `fps ${s.fps}`);
  assert.equal(s.jankCount, 0);
  assert.equal(s.overBudgetRatio, 0);
  assert.equal(s.droppedFrames, 0);
});

test("a long frame is counted over-budget, janky, and as a dropped frame", () => {
  const fs = new FrameStats({ budgetMs: 16, jankFactor: 2 });
  fs.record(16); // on budget
  fs.record(50); // > 2× budget → janky; floor(50/16)-1 = 2 dropped
  const s = fs.snapshot();
  assert.equal(s.jankCount, 1);
  assert.equal(s.overBudgetRatio, 0.5);
  assert.equal(s.droppedFrames, 2);
  assert.equal(s.lifetime.jank, 1);
  assert.equal(s.lifetime.dropped, 2);
});

test("non-finite / negative durations are ignored", () => {
  const fs = new FrameStats();
  fs.record(NaN);
  fs.record(-5);
  fs.record(Infinity);
  assert.equal(fs.hasData, false);
});

test("the rolling window evicts old frames but lifetime totals persist", () => {
  const fs = new FrameStats({ windowSize: 3, budgetMs: 16, jankFactor: 2 });
  fs.record(50); // janky — will be evicted from the window
  fs.record(16);
  fs.record(16);
  fs.record(16); // evicts the first (50ms) frame
  const s = fs.snapshot();
  assert.equal(s.count, 3);
  assert.equal(s.jankCount, 0, "the janky frame fell out of the window");
  assert.equal(s.lifetime.jank, 1, "but lifetime remembers it");
  assert.deepEqual(fs.toArray(), [16, 16, 16]);
});

test("p95 is the high-percentile frame, max is the worst", () => {
  const fs = new FrameStats({ windowSize: 100 });
  for (let i = 0; i < 99; i++) fs.record(10);
  fs.record(100); // one outlier
  const s = fs.snapshot();
  assert.equal(s.maxMs, 100);
  // 99% of frames are 10ms; p95 sits in the 10ms band, not at the outlier.
  assert.ok(s.p95Ms < 100, `p95 ${s.p95Ms}`);
});

test("reset clears window and lifetime", () => {
  const fs = new FrameStats();
  fs.record(50);
  fs.reset();
  const s = fs.snapshot();
  assert.equal(s.count, 0);
  assert.equal(s.lifetime.frames, 0);
});

test("percentile interpolates and clamps q", () => {
  assert.equal(percentile([], 0.5), 0);
  assert.equal(percentile([5], 0.9), 5);
  assert.equal(percentile([0, 10], 0.5), 5);
  assert.equal(percentile([0, 10], 2), 10); // q clamped to 1
  assert.equal(percentile([0, 10], -1), 0); // q clamped to 0
});
