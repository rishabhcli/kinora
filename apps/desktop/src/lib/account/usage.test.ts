import { describe, it, expect } from "vitest";
import {
  meter,
  usageMeters,
  formatSecondsMeter,
  formatCountMeter,
  shouldNudgeUpgrade,
} from "./usage";
import { FREE_PLAN, findPlan } from "./billing";

describe("meter", () => {
  it("computes a clamped fraction", () => {
    expect(meter(30, 60).fraction).toBe(0.5);
    expect(meter(90, 60).fraction).toBe(1); // clamped
    expect(meter(-5, 60).used).toBe(0); // clamped low
  });
  it("flags nearLimit and exhausted", () => {
    expect(meter(48, 60).nearLimit).toBe(true); // 0.8
    expect(meter(40, 60).nearLimit).toBe(false);
    expect(meter(60, 60).exhausted).toBe(true);
    expect(meter(70, 60).exhausted).toBe(true);
  });
  it("treats Infinity as unlimited (never near/exhausted)", () => {
    const m = meter(9999, Infinity);
    expect(m.unlimited).toBe(true);
    expect(m.fraction).toBe(0);
    expect(m.nearLimit).toBe(false);
    expect(m.exhausted).toBe(false);
  });
  it("a zero limit is immediately full", () => {
    const m = meter(0, 0);
    expect(m.fraction).toBe(1);
    expect(m.exhausted).toBe(true);
  });
});

describe("usageMeters", () => {
  it("builds three meters from a snapshot + entitlements", () => {
    const m = usageMeters(
      { videoSeconds: 300, directorEdits: 5, concurrentFilms: 1 },
      FREE_PLAN.entitlements,
    );
    expect(m.videoSeconds.fraction).toBeCloseTo(0.5); // 300 / 600
    expect(m.concurrentFilms.exhausted).toBe(true); // 1 / 1
  });
  it("studio entitlements read as unlimited", () => {
    const studio = findPlan("studio")!;
    const m = usageMeters({ videoSeconds: 1e6, directorEdits: 999, concurrentFilms: 99 }, studio.entitlements);
    expect(m.videoSeconds.unlimited).toBe(true);
    expect(m.directorEdits.unlimited).toBe(true);
  });
});

describe("formatting", () => {
  it("seconds meter → minutes label", () => {
    expect(formatSecondsMeter(meter(120, 600))).toBe("2 / 10 min");
    expect(formatSecondsMeter(meter(120, Infinity))).toBe("2 min used · unlimited");
  });
  it("count meter label", () => {
    expect(formatCountMeter(meter(3, 5))).toBe("3 / 5");
    expect(formatCountMeter(meter(3, Infinity))).toBe("3 · unlimited");
  });
});

describe("shouldNudgeUpgrade", () => {
  it("is true when any meter is near/at its cap", () => {
    const at = usageMeters({ videoSeconds: 600, directorEdits: 0, concurrentFilms: 0 }, FREE_PLAN.entitlements);
    expect(shouldNudgeUpgrade(at)).toBe(true);
    const ok = usageMeters({ videoSeconds: 60, directorEdits: 1, concurrentFilms: 0 }, FREE_PLAN.entitlements);
    expect(shouldNudgeUpgrade(ok)).toBe(false);
  });
});
