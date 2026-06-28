import { describe, it, expect } from "vitest";
import { createHistoryStore, type KeyValueStore } from "./history";

function memStore(seed?: Record<string, string>): KeyValueStore {
  const m = new Map(Object.entries(seed ?? {}));
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v) };
}

describe("createHistoryStore", () => {
  it("records and reads back interactions oldest-first", () => {
    let t = 1000;
    const h = createHistoryStore(memStore(), { now: () => t++ });
    h.record("a", "open", { genre: "SF" });
    h.record("b", "hover");
    expect(h.all().map((r) => r.bookId)).toEqual(["a", "b"]);
    expect(h.all()[0].genre).toBe("SF");
  });

  it("uses an explicit timestamp when provided", () => {
    const h = createHistoryStore(memStore());
    h.record("a", "open", {}, 42);
    expect(h.all()[0].at).toBe(42);
  });

  it("filters by book and finds the last event", () => {
    let t = 1;
    const h = createHistoryStore(memStore(), { now: () => t++ });
    h.record("a", "view");
    h.record("a", "open");
    h.record("b", "view");
    expect(h.forBook("a").map((r) => r.kind)).toEqual(["view", "open"]);
    expect(h.lastFor("a")!.kind).toBe("open");
    expect(h.lastFor("z")).toBeNull();
  });

  it("trims to the ring-buffer max (keeps newest)", () => {
    let t = 0;
    const h = createHistoryStore(memStore(), { now: () => t++, max: 3 });
    for (const id of ["a", "b", "c", "d", "e"]) h.record(id, "view");
    expect(h.all().map((r) => r.bookId)).toEqual(["c", "d", "e"]);
  });

  it("clear empties the log", () => {
    const h = createHistoryStore(memStore());
    h.record("a", "open");
    h.clear();
    expect(h.all()).toEqual([]);
  });

  it("tolerates corrupt persisted JSON", () => {
    const h = createHistoryStore(memStore({ "kinora.discovery.history.v1": "{not json" }));
    expect(h.all()).toEqual([]);
    h.record("a", "view");
    expect(h.all().length).toBe(1);
  });

  it("drops malformed records on read", () => {
    const bad = JSON.stringify([{ bookId: "ok", kind: "open", at: 1 }, { nope: true }]);
    const h = createHistoryStore(memStore({ "kinora.discovery.history.v1": bad }));
    expect(h.all().map((r) => r.bookId)).toEqual(["ok"]);
  });
});
