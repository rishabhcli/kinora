import { describe, it, expect } from "vitest";
import { createRecentsStore, resolveRecents } from "./recents";
import type { KeyValueStore } from "./history";

function memStore(seed?: Record<string, string>): KeyValueStore {
  const m = new Map(Object.entries(seed ?? {}));
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v) };
}

describe("createRecentsStore", () => {
  it("pushes MRU-first and dedupes", () => {
    const r = createRecentsStore(memStore());
    r.push("a");
    r.push("b");
    r.push("a"); // re-push moves to front
    expect(r.list()).toEqual(["a", "b"]);
  });

  it("caps the buffer", () => {
    const r = createRecentsStore(memStore(), 2);
    r.push("a");
    r.push("b");
    r.push("c");
    expect(r.list()).toEqual(["c", "b"]);
  });

  it("clears", () => {
    const r = createRecentsStore(memStore());
    r.push("a");
    r.clear();
    expect(r.list()).toEqual([]);
  });

  it("tolerates corrupt JSON", () => {
    const r = createRecentsStore(memStore({ "kinora.discovery.recents.v1": "nope" }));
    expect(r.list()).toEqual([]);
  });
});

describe("resolveRecents", () => {
  it("maps ids to books in MRU order, skipping missing", () => {
    const books = [{ id: "a" }, { id: "b" }, { id: "c" }];
    expect(resolveRecents(["c", "x", "a"], books).map((b) => b.id)).toEqual(["c", "a"]);
  });
});
