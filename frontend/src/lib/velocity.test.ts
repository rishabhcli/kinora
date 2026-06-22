import { describe, expect, it } from "vitest";

import { DEFAULT_WPS, VelocityTracker, velocityBounds } from "./velocity";

describe("velocityBounds", () => {
  it("clamps to [0.5x, 3x] of the default wps", () => {
    expect(velocityBounds(4)).toEqual({ min: 2, max: 12 });
  });
});

describe("VelocityTracker", () => {
  it("starts at the default reading speed before any movement", () => {
    const v = new VelocityTracker();
    expect(v.value).toBe(DEFAULT_WPS);
  });

  it("ignores the very first sample (it only anchors)", () => {
    const v = new VelocityTracker();
    expect(v.sample(100, 0)).toBe(DEFAULT_WPS);
  });

  it("clamps a single trackpad flick to the ceiling (no spike)", () => {
    const v = new VelocityTracker();
    v.sample(0, 0);
    // 50,000 words in 50ms is an absurd flick.
    const reported = v.sample(50_000, 50);
    expect(reported).toBe(12);
    expect(v.value).toBeLessThanOrEqual(12);
  });

  it("floors a sustained slow read at 0.5x the default", () => {
    const v = new VelocityTracker();
    let t = 0;
    let word = 0;
    // ~1 wps, well under the 2 wps floor, sampled for a long time.
    for (let i = 0; i < 200; i += 1) {
      t += 1000;
      word += 1;
      v.sample(word, t);
    }
    expect(v.value).toBe(2);
  });

  it("converges toward a steady reading rate inside the band", () => {
    const v = new VelocityTracker();
    let t = 0;
    let word = 0;
    for (let i = 0; i < 200; i += 1) {
      t += 250;
      word += 1; // 4 wps
      v.sample(word, t);
    }
    expect(v.value).toBeGreaterThan(3.5);
    expect(v.value).toBeLessThan(4.5);
  });

  it("tracks direction and resets to default", () => {
    const v = new VelocityTracker();
    v.sample(100, 0);
    v.sample(80, 500);
    expect(v.direction).toBe(-1);
    v.reset();
    expect(v.value).toBe(DEFAULT_WPS);
    expect(v.direction).toBe(1);
  });
});
