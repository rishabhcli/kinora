// useOfflineCache: the page-side reducer + the no-SW-support path — vitest.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { applyMessage, useOfflineCache, type OfflineStatus } from "./useOfflineCache";

describe("applyMessage reducer", () => {
  const run = (...msgs: Parameters<typeof applyMessage>[0][]) => {
    let s: OfflineStatus = { supported: true, active: true, progress: null, clips: 0, pages: 0, error: null };
    for (const m of msgs) applyMessage(m, (fn) => (s = fn(s)));
    return s;
  };

  it("PRECACHE_PROGRESS sets a 0..1 progress", () => {
    expect(run({ type: "PRECACHE_PROGRESS", bookId: "b", done: 1, total: 4 }).progress).toBe(0.25);
  });

  it("PRECACHE_DONE pins progress to 1", () => {
    expect(run({ type: "PRECACHE_DONE", bookId: "b", cached: 4, failed: 0 }).progress).toBe(1);
  });

  it("STATUS updates the cached counts", () => {
    const s = run({ type: "STATUS", bookId: "b", clips: 3, pages: 7, bytes: 0 });
    expect(s.clips).toBe(3);
    expect(s.pages).toBe(7);
  });

  it("EVICTED clears counts and progress", () => {
    const s = run(
      { type: "STATUS", bookId: "b", clips: 3, pages: 7, bytes: 0 },
      { type: "EVICTED", scope: "book", bookId: "b" },
    );
    expect(s.clips).toBe(0);
    expect(s.pages).toBe(0);
    expect(s.progress).toBeNull();
  });
});

describe("useOfflineCache without service-worker support", () => {
  let original: PropertyDescriptor | undefined;
  beforeEach(() => {
    original = Object.getOwnPropertyDescriptor(navigator, "serviceWorker");
    // Force "no SW" by removing the property.
    Object.defineProperty(navigator, "serviceWorker", { value: undefined, configurable: true });
  });
  afterEach(() => {
    if (original) Object.defineProperty(navigator, "serviceWorker", original);
    vi.restoreAllMocks();
  });

  it("reports unsupported and no-ops on precache/evict (no throw)", () => {
    const { result } = renderHook(() => useOfflineCache());
    expect(result.current.status.supported).toBe(false);
    expect(() => {
      result.current.precache({ bookId: "b", clipUrls: [], pageUrls: [], plannedBytes: 0, droppedForBudget: [] });
      result.current.evictBook("b");
      result.current.evictAll();
    }).not.toThrow();
  });
});
