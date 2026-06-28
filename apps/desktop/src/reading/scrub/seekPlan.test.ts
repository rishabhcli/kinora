// Seek planner — vitest (imports sibling frameClock).
import { describe, expect, it } from "vitest";
import { planSeek } from "./seekPlan";

const C30 = { fps: 30, durationS: 10 };

describe("planSeek", () => {
  it("scrubbing issues a continuous (un-quantized) seek past the epsilon", () => {
    const p = planSeek({ targetTimeS: 2.0, currentTimeS: 1.0, mode: "scrub", clock: C30 });
    expect(p.seekToS).toBe(2.0);
    expect(p.quantized).toBe(false);
  });

  it("scrubbing within the epsilon skips (no thrash)", () => {
    const p = planSeek({ targetTimeS: 1.0, currentTimeS: 1.0 + 1 / 60, mode: "scrub", clock: C30 });
    expect(p.seekToS).toBeNull();
  });

  it("settling quantizes to a frame centre", () => {
    const p = planSeek({ targetTimeS: 2.04, currentTimeS: 0, mode: "settle", clock: C30 });
    expect(p.quantized).toBe(true);
    // 2.04s @30fps → frame 61, centre 61.5/30 = 2.05s.
    expect(p.frameIndex).toBe(61);
    expect(p.seekToS).toBeCloseTo(61.5 / 30, 6);
  });

  it("settling onto the same frame we're already on skips", () => {
    // currentTime 2.04 and target 2.05 are both frame 61 → no re-seek.
    const p = planSeek({ targetTimeS: 2.05, currentTimeS: 2.04, mode: "settle", clock: C30 });
    expect(p.seekToS).toBeNull();
  });

  it("unknown fps never quantizes (falls back to a plain gated seek)", () => {
    const p = planSeek({ targetTimeS: 3, currentTimeS: 0, mode: "settle", clock: { fps: 0, durationS: 8 } });
    expect(p.quantized).toBe(false);
    expect(p.seekToS).toBe(3);
  });

  it("a non-finite target is skipped", () => {
    const p = planSeek({ targetTimeS: NaN, currentTimeS: 0, mode: "scrub", clock: C30 });
    expect(p.seekToS).toBeNull();
  });
});
