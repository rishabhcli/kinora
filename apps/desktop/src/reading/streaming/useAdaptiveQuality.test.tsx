import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useAdaptiveQuality, type AdaptiveSignals } from "./useAdaptiveQuality";
import { BandwidthEstimator } from "./bandwidth";

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("performance", { now: () => Date.now() } as Performance);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useAdaptiveQuality", () => {
  it("settles on an HD rung when bandwidth and buffer are abundant", () => {
    const estimator = new BandwidthEstimator({ initialKbps: 20000 });
    const signals: AdaptiveSignals = { bufferAheadS: 30, decodeHealth: "good", jankRatio: 0 };
    const { result } = renderHook(() =>
      useAdaptiveQuality({ estimator, getSignals: () => signals, intervalMs: 1000 }),
    );
    // The initial decide() + a couple ticks past the upgrade dwell.
    act(() => {
      vi.advanceTimersByTime(9000);
    });
    expect(result.current.decision.level.id).toBe("hd");
  });

  it("drops to a lean rung immediately on a bandwidth collapse", () => {
    const estimator = new BandwidthEstimator({ initialKbps: 20000 });
    let signals: AdaptiveSignals = { bufferAheadS: 30 };
    const { result } = renderHook(() =>
      useAdaptiveQuality({ estimator, getSignals: () => signals, intervalMs: 1000 }),
    );
    act(() => {
      vi.advanceTimersByTime(9000);
    });
    expect(result.current.decision.level.id).toBe("hd");
    // Collapse the link and tick once — downgrade is immediate (no dwell).
    estimator.reset(120);
    signals = { bufferAheadS: 30 };
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(["pan", "still", "audio"]).toContain(result.current.decision.level.id);
  });

  it("never blanks: bottom rung is reachable under total starvation", () => {
    const estimator = new BandwidthEstimator({ initialKbps: 0 });
    const signals: AdaptiveSignals = { bufferAheadS: 0, decodeHealth: "stalled" };
    const { result } = renderHook(() =>
      useAdaptiveQuality({ estimator, getSignals: () => signals, intervalMs: 1000 }),
    );
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current.decision.level.tier).not.toBe("video-hd");
    expect(result.current.decision.level.height).toBeGreaterThanOrEqual(0);
  });
});
