// GPU transition curves — vitest (imports sibling grade).
import { describe, expect, it } from "vitest";
import { transitionAt, sampleTransition, easeInOut, smoothstep, midBell } from "./transitions";
import { NEUTRAL_GRADE } from "./grade";

describe("transitions", () => {
  it("easing curves are monotonic 0→1 and pinned at the ends", () => {
    expect(easeInOut(0)).toBe(0);
    expect(easeInOut(1)).toBe(1);
    expect(smoothstep(0)).toBe(0);
    expect(smoothstep(1)).toBe(1);
    expect(easeInOut(0.3)).toBeLessThan(easeInOut(0.7));
  });

  it("midBell peaks at 0.5 and is zero at the ends", () => {
    expect(midBell(0)).toBeCloseTo(0);
    expect(midBell(1)).toBeCloseTo(0);
    expect(midBell(0.5)).toBeCloseTo(1);
  });

  it("every transition keeps mix monotonic 0→1 with both layers present (no black)", () => {
    for (const kind of ["dissolve", "soft-dissolve", "bloom", "desat-dip"] as const) {
      const frames = sampleTransition(kind, 12);
      expect(frames[0].mix).toBeCloseTo(0);
      expect(frames[frames.length - 1].mix).toBeCloseTo(1);
      // mix never decreases — both layers stay drawn; the compositor never blanks.
      for (let i = 1; i < frames.length; i++) {
        expect(frames[i].mix).toBeGreaterThanOrEqual(frames[i - 1].mix - 1e-9);
      }
    }
  });

  it("desat-dip dips saturation at the midpoint but recovers at the ends", () => {
    const mid = transitionAt("desat-dip", 0.5, NEUTRAL_GRADE);
    const end = transitionAt("desat-dip", 1, NEUTRAL_GRADE);
    expect(mid.grade.saturation).toBeLessThan(NEUTRAL_GRADE.saturation);
    expect(end.grade.saturation).toBeCloseTo(NEUTRAL_GRADE.saturation);
  });

  it("bloom lifts exposure (gain) mid-cut without going to black", () => {
    const mid = transitionAt("bloom", 0.5, NEUTRAL_GRADE);
    expect(mid.grade.gain[0]).toBeGreaterThan(NEUTRAL_GRADE.gain[0]);
  });

  it("cut is degenerate: 0 until the end then 1", () => {
    expect(transitionAt("cut", 0.4).mix).toBe(0);
    expect(transitionAt("cut", 1).mix).toBe(1);
  });
});
