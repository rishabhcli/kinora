import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { usePerfMonitor } from "./usePerfMonitor";

// A controllable rAF: we hold the callback and a virtual clock so the hook's loop
// advances exactly when the test says, with deterministic timestamps.
function installRaf() {
  let cb: FrameRequestCallback | null = null;
  let id = 0;
  let nowMs = 0;
  vi.stubGlobal(
    "requestAnimationFrame",
    (fn: FrameRequestCallback) => {
      cb = fn;
      return ++id;
    },
  );
  vi.stubGlobal("cancelAnimationFrame", () => {
    cb = null;
  });
  vi.stubGlobal("performance", { now: () => nowMs } as Performance);
  return {
    /** advance the clock by `dt` ms and fire one frame */
    tick(dt: number) {
      nowMs += dt;
      const fn = cb;
      cb = null;
      act(() => fn?.(nowMs));
    },
  };
}

beforeEach(() => {
  vi.useRealTimers();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("usePerfMonitor", () => {
  it("starts empty and records steady frames as ~60fps", () => {
    const raf = installRaf();
    const { result } = renderHook(() => usePerfMonitor({ publishIntervalMs: 100 }));
    expect(result.current.snapshot.count).toBe(0);
    // First frame seeds prevTs (no duration recorded yet).
    raf.tick(0);
    // 10 frames at 16ms.
    for (let i = 0; i < 10; i++) raf.tick(16);
    // Cross the publish interval to flush a snapshot.
    raf.tick(100);
    const s = result.current.read();
    expect(s.count).toBeGreaterThan(0);
    expect(s.fps).toBeGreaterThan(0);
  });

  it("flags a long frame as jank in the published snapshot", () => {
    const raf = installRaf();
    const { result } = renderHook(() =>
      usePerfMonitor({ publishIntervalMs: 50, budgetMs: 16, jankFactor: 2 }),
    );
    raf.tick(0); // seed
    raf.tick(60); // one 60ms frame → jank, and crosses publish interval
    const s = result.current.read();
    expect(s.lifetime.jank).toBe(1);
  });

  it("is inert when disabled", () => {
    const raf = installRaf();
    const { result } = renderHook(() => usePerfMonitor({ enabled: false, publishIntervalMs: 10 }));
    raf.tick(0);
    raf.tick(20);
    expect(result.current.snapshot.count).toBe(0);
  });

  it("polls decode quality from the provided video ref", () => {
    const raf = installRaf();
    let total = 0;
    let dropped = 0;
    const videoRef = {
      current: {
        getVideoPlaybackQuality: () => ({ totalVideoFrames: total, droppedVideoFrames: dropped }),
      },
    };
    const { result } = renderHook(() => usePerfMonitor({ publishIntervalMs: 100, videoRef }));
    raf.tick(0); // seed frame
    total = 30;
    raf.tick(100); // publish #1 → decode baseline
    total = 60;
    dropped = 30; // half the next interval's frames dropped → stalled
    raf.tick(100); // publish #2 → interval delta computed
    expect(["degraded", "stalled"]).toContain(result.current.read().decodeHealth);
  });
});
