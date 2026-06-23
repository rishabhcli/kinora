import { describe, expect, it } from "vitest";

import { LruMap } from "./lruMap";

describe("LruMap", () => {
  it("evicts the least-recently-used entry past the cap", () => {
    const m = new LruMap<string, number>(2);
    m.set("a", 1);
    m.set("b", 2);
    m.get("a"); // touch a → most-recent; b is now the LRU
    m.set("c", 3); // over cap → evict b
    expect(m.has("a")).toBe(true);
    expect(m.has("b")).toBe(false);
    expect(m.has("c")).toBe(true);
    expect(m.size).toBe(2);
  });

  it("peek reads without refreshing recency", () => {
    const m = new LruMap<string, number>(2);
    m.set("a", 1);
    m.set("b", 2);
    m.peek("a"); // does NOT make a most-recent
    m.set("c", 3); // a is still the LRU → evicted
    expect(m.has("a")).toBe(false);
    expect(m.has("b")).toBe(true);
    expect(m.has("c")).toBe(true);
  });

  it("set on an existing key updates the value and refreshes recency", () => {
    const m = new LruMap<string, number>(2);
    m.set("a", 1);
    m.set("b", 2);
    m.set("a", 10); // update + most-recent
    m.set("c", 3); // evict b (now LRU)
    expect(m.get("a")).toBe(10);
    expect(m.has("b")).toBe(false);
  });

  it("clamps a non-positive cap to 1", () => {
    const m = new LruMap<string, number>(0);
    m.set("a", 1);
    m.set("b", 2);
    expect(m.size).toBe(1);
    expect(m.has("b")).toBe(true);
  });

  it("is a real Map (iteration + values still work)", () => {
    const m = new LruMap<string, number>(4);
    m.set("a", 1);
    m.set("b", 2);
    expect([...m.values()].sort()).toEqual([1, 2]);
    expect(m instanceof Map).toBe(true);
  });
});
