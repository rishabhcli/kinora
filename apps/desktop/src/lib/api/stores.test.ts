import { describe, it, expect, beforeEach } from "vitest";
import {
  analyticsStore,
  annotationStore,
  collectionStore,
  recordReading,
  __resetStoresForTests,
} from "./stores";
import { summarize } from "./analytics";

beforeEach(() => {
  __resetStoresForTests();
  // The singletons are localStorage-backed; the jsdom setup provides a fresh
  // in-memory storage per test file, but reset ensures a clean in-memory copy.
  try {
    localStorage.clear();
  } catch {
    /* no storage */
  }
});

describe("store singletons", () => {
  it("returns the same instance across calls", () => {
    expect(analyticsStore()).toBe(analyticsStore());
    expect(annotationStore()).toBe(annotationStore());
    expect(collectionStore()).toBe(collectionStore());
  });

  it("resets to fresh instances", () => {
    const a = analyticsStore();
    __resetStoresForTests();
    expect(analyticsStore()).not.toBe(a);
  });
});

describe("recordReading (cross-domain seam)", () => {
  it("writes a reading event the analytics summary then reflects", () => {
    recordReading("b1", 300, 60, 1000);
    recordReading("b1", 150, 30, 2000);
    const events = analyticsStore().events();
    expect(events).toHaveLength(2);

    const summary = summarize(events, []);
    expect(summary.totalWords).toBe(450);
    expect(summary.totalSeconds).toBe(90);
    expect(summary.avgWpm).toBe(300);
  });

  it("drops a non-positive-time sample", () => {
    recordReading("b1", 100, 0);
    expect(analyticsStore().events()).toHaveLength(0);
  });
});
