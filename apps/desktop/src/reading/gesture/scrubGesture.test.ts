// Pure touch/pointer scrub-gesture model — node:test.
import test from "node:test";
import assert from "node:assert/strict";
import { ScrubGesture } from "./scrubGesture.ts";

test("a vertical drag up advances the timeline (natural direction)", () => {
  const g = new ScrubGesture({ pxPerFullScrub: 1000 });
  g.begin({ x: 0, y: 500, t: 0 });
  // Drag up 100px → forward 100/1000 = 0.1 of the timeline.
  const d = g.move({ x: 0, y: 400, t: 100 });
  assert.ok(Math.abs(d.fractionDelta - 0.1) < 1e-9);
});

test("invert flips the direction", () => {
  const g = new ScrubGesture({ pxPerFullScrub: 1000, invert: true });
  g.begin({ x: 0, y: 500, t: 0 });
  const d = g.move({ x: 0, y: 400, t: 100 }); // up, but inverted → backward
  assert.ok(d.fractionDelta < 0);
});

test("move before begin is a no-op", () => {
  const g = new ScrubGesture();
  const d = g.move({ x: 0, y: 0, t: 0 });
  assert.equal(d.fractionDelta, 0);
});

test("a fast release produces a fling; a slow one does not", () => {
  const fast = new ScrubGesture({ pxPerFullScrub: 1000, flingMinVelocity: 80 });
  fast.begin({ x: 0, y: 500, t: 0 });
  fast.move({ x: 0, y: 100, t: 100 }); // 400px up in 100ms → 4000 px/s
  const flung = fast.end();
  assert.equal(flung.fling, true);
  assert.ok(flung.velocityFractionPerSec > 0);

  const slow = new ScrubGesture({ pxPerFullScrub: 1000, flingMinVelocity: 80 });
  slow.begin({ x: 0, y: 500, t: 0 });
  slow.move({ x: 0, y: 498, t: 500 }); // 2px in 500ms → 4 px/s
  assert.equal(slow.end().fling, false);
});

test("stepFling decays velocity toward done", () => {
  const g = new ScrubGesture({ flingFriction: 0.0025 });
  let v = 2;
  let steps = 0;
  for (; steps < 100; steps++) {
    const r = g.stepFling(v, 1 / 60);
    v = r.velocity;
    if (r.done) break;
  }
  assert.ok(steps < 100, "fling settled");
  assert.ok(Math.abs(v) < 1e-3);
});

test("isActive tracks begin/end", () => {
  const g = new ScrubGesture();
  assert.equal(g.isActive, false);
  g.begin({ x: 0, y: 0, t: 0 });
  assert.equal(g.isActive, true);
  g.end();
  assert.equal(g.isActive, false);
});
