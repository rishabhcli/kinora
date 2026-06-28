import { describe, it, expect } from "vitest";
import {
  memoryStore,
  resolveStore,
  readJson,
  writeJson,
  removeKey,
  insecureRandomBytes,
  webRandomBytes,
} from "./store";

describe("memoryStore", () => {
  it("seeds, gets, sets, and removes", () => {
    const s = memoryStore({ a: "1" });
    expect(s.getItem("a")).toBe("1");
    expect(s.getItem("missing")).toBeNull();
    s.setItem("b", "2");
    expect(s.getItem("b")).toBe("2");
    s.removeItem?.("a");
    expect(s.getItem("a")).toBeNull();
  });
});

describe("resolveStore", () => {
  it("prefers the caller's store", () => {
    const s = memoryStore();
    expect(resolveStore(s)).toBe(s);
  });
  it("falls back to a usable store when none given", () => {
    const s = resolveStore(null);
    s.setItem("x", "y");
    expect(s.getItem("x")).toBe("y");
  });
});

describe("readJson / writeJson", () => {
  it("round-trips a value", () => {
    const s = memoryStore();
    expect(writeJson(s, "k", { n: 1 })).toBe(true);
    expect(readJson(s, "k", null)).toEqual({ n: 1 });
  });
  it("returns the fallback for missing or corrupt data", () => {
    const s = memoryStore({ bad: "{not json" });
    expect(readJson(s, "absent", "fb")).toBe("fb");
    expect(readJson(s, "bad", "fb")).toBe("fb");
  });
  it("writeJson reports false when the store throws", () => {
    const throwing = { getItem: () => null, setItem: () => { throw new Error("quota"); } };
    expect(writeJson(throwing, "k", 1)).toBe(false);
  });
  it("readJson returns fallback when getItem throws", () => {
    const throwing = { getItem: () => { throw new Error("blocked"); }, setItem: () => {} };
    expect(readJson(throwing, "k", 42)).toBe(42);
  });
});

describe("removeKey", () => {
  it("uses removeItem when present", () => {
    const s = memoryStore({ a: "1" });
    removeKey(s, "a");
    expect(s.getItem("a")).toBeNull();
  });
  it("blanks the value when removeItem is absent", () => {
    const map = new Map([["a", "1"]]);
    const s = { getItem: (k: string) => map.get(k) ?? null, setItem: (k: string, v: string) => void map.set(k, v) };
    removeKey(s, "a");
    expect(s.getItem("a")).toBe("");
  });
});

describe("random bytes", () => {
  it("insecureRandomBytes returns the requested length", () => {
    expect(insecureRandomBytes(16)).toHaveLength(16);
  });
  it("webRandomBytes returns a function producing the requested length", () => {
    const rand = webRandomBytes();
    expect(rand(8)).toHaveLength(8);
  });
});
