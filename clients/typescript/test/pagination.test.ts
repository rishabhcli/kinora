import { describe, it, expect } from "vitest";
import { Page } from "../src/pagination.js";

describe("Page", () => {
  const items = [1, 2, 3, 4, 5];

  it("exposes items, length, and first", () => {
    const p = new Page(items);
    expect(p.length).toBe(5);
    expect(p.first()).toBe(1);
    expect(p.collect()).toEqual(items);
  });

  it("is synchronously iterable", () => {
    const p = new Page(items);
    expect([...p]).toEqual(items);
  });

  it("is asynchronously iterable", async () => {
    const p = new Page(items);
    const out: number[] = [];
    for await (const x of p) out.push(x);
    expect(out).toEqual(items);
  });

  it("chunks by pageSize", () => {
    const p = new Page(items, 2);
    expect([...p.chunks()]).toEqual([[1, 2], [3, 4], [5]]);
  });

  it("map and filter preserve page metadata", () => {
    const p = new Page(items, 3);
    const mapped = p.map((x) => x * 2);
    expect(mapped.collect()).toEqual([2, 4, 6, 8, 10]);
    expect(mapped.pageSize).toBe(3);
    const filtered = p.filter((x) => x % 2 === 0);
    expect(filtered.collect()).toEqual([2, 4]);
  });

  it("clamps pageSize to >= 1", () => {
    expect(new Page(items, 0).pageSize).toBe(1);
    expect(new Page(items, -5).pageSize).toBe(1);
  });

  it("first is undefined on an empty page", () => {
    expect(new Page<number>([]).first()).toBeUndefined();
  });
});
