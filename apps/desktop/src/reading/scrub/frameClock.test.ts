// Pure frame-accurate scrub math — node:test.
import test from "node:test";
import assert from "node:assert/strict";
import { frameCount, quantizeTime, quantizePosition, stepFrames, sameFrame } from "./frameClock.ts";

const C30 = { fps: 30, durationS: 10 }; // 300 frames

test("frameCount = floor(duration*fps), at least 1", () => {
  assert.equal(frameCount(C30), 300);
  assert.equal(frameCount({ fps: 24, durationS: 2 }), 48);
  assert.equal(frameCount({ fps: 0, durationS: 10 }), 1);
  assert.equal(frameCount({ fps: 30, durationS: 0 }), 1);
});

test("quantizeTime lands on a frame centre and is repeatable", () => {
  // 1.0s at 30fps → frame 30, centre (30+0.5)/30 = 1.0167s.
  const a = quantizeTime(C30, 1.0);
  assert.equal(a.index, 30);
  assert.ok(Math.abs(a.timeS - 30.5 / 30) < 1e-9);
  // A time slightly inside the same frame quantises identically (no shimmer).
  const b = quantizeTime(C30, 1.01);
  assert.equal(b.index, a.index);
  assert.equal(b.timeS, a.timeS);
});

test("quantizeTime clamps to the clip's frame range", () => {
  const last = quantizeTime(C30, 999);
  assert.equal(last.index, 299);
  assert.ok(last.timeS <= C30.durationS);
  const first = quantizeTime(C30, -5);
  assert.equal(first.index, 0);
});

test("quantizePosition maps 0..1 across the frames", () => {
  assert.equal(quantizePosition(C30, 0).index, 0);
  assert.equal(quantizePosition(C30, 1).index, 299);
  assert.equal(quantizePosition(C30, 0.5).index, 150);
});

test("stepFrames moves whole frames and clamps", () => {
  const start = quantizeTime(C30, 5.0); // frame 150
  const fwd = stepFrames(C30, start.timeS, 3);
  assert.equal(fwd.index, 153);
  const back = stepFrames(C30, start.timeS, -1000);
  assert.equal(back.index, 0);
});

test("sameFrame is true within a frame, false across frames", () => {
  assert.equal(sameFrame(C30, 1.0, 1.02), true); // both frame 30
  assert.equal(sameFrame(C30, 1.0, 1.05), false); // frame 30 vs 31
});

test("unknown fps degrades gracefully (single frame, time passthrough)", () => {
  const c = { fps: 0, durationS: 8 };
  const q = quantizeTime(c, 3);
  assert.equal(q.index, 0);
  assert.equal(q.total, 1);
  assert.equal(q.timeS, 3);
});
