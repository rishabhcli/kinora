import { describe, it, expect } from "vitest";
import {
  createAnnotationStore,
  anchorIsValid,
  sortThreads,
  threadsForShot,
  threadsInWordRange,
  countThreads,
  type KeyValueStore,
  type Clock,
  type IdGen,
  type AnnotationThread,
} from "./annotations";

const memStore = (seed?: Record<string, string>): KeyValueStore => {
  const m = new Map(Object.entries(seed ?? {}));
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v) };
};

// Deterministic clock + ids so tests are fully reproducible.
function deps(start = 1000): { clock: Clock; ids: IdGen } {
  let t = start;
  let n = 0;
  return {
    clock: { now: () => (t += 1) },
    ids: { next: (p) => `${p}${n++}` },
  };
}

describe("anchorIsValid", () => {
  it("requires at least one anchor and a sane word range", () => {
    expect(anchorIsValid({ shot_id: "s1" })).toBe(true);
    expect(anchorIsValid({ word_range: [10, 20] })).toBe(true);
    expect(anchorIsValid({ word_range: [20, 10] })).toBe(false);
    expect(anchorIsValid({})).toBe(false);
  });
});

describe("annotation store: open / reply / resolve", () => {
  it("opens a thread with a first comment, replies, resolves", () => {
    const store = createAnnotationStore(memStore(), deps());
    const t = store.open("book1", { shot_id: "shot1" }, "Ada", "Make this warmer", ["look"]);
    expect(t.comments).toHaveLength(1);
    expect(t.comments[0].body).toBe("Make this warmer");
    expect(t.resolved).toBe(false);

    const replied = store.reply(t.id, "Grace", "Agreed, +warmth")!;
    expect(replied.comments).toHaveLength(2);
    expect(replied.comments[1].author).toBe("Grace");

    const resolved = store.setResolved(t.id, true, "Ada")!;
    expect(resolved.resolved).toBe(true);
    expect(resolved.resolved_by).toBe("Ada");
  });

  it("edits a comment and stamps edited_at", () => {
    const store = createAnnotationStore(memStore(), deps());
    const t = store.open("b", { shot_id: "s" }, "Ada", "typo");
    const cid = t.comments[0].id;
    const edited = store.editComment(t.id, cid, "fixed")!;
    expect(edited.comments[0].body).toBe("fixed");
    expect(edited.comments[0].edited_at).toBeGreaterThan(0);
  });

  it("persists and rehydrates across stores", () => {
    const backing = memStore();
    const a = createAnnotationStore(backing, deps());
    a.open("book1", { shot_id: "s" }, "Ada", "note");
    const b = createAnnotationStore(backing, deps());
    expect(b.forBook("book1")).toHaveLength(1);
  });

  it("removes a thread", () => {
    const store = createAnnotationStore(memStore(), deps());
    const t = store.open("b", { shot_id: "s" }, "Ada", "x");
    store.remove(t.id);
    expect(store.forBook("b")).toHaveLength(0);
  });
});

describe("query helpers", () => {
  const threads: AnnotationThread[] = [
    { id: "1", book_id: "b", anchor: { shot_id: "s1", word_range: [0, 10] }, comments: [], resolved: false, tags: [], createdAt: 1, updatedAt: 5 },
    { id: "2", book_id: "b", anchor: { word_range: [50, 60] }, comments: [], resolved: true, tags: [], createdAt: 1, updatedAt: 9 },
    { id: "3", book_id: "b", anchor: { shot_id: "s1", word_range: [5, 15] }, comments: [], resolved: false, tags: [], createdAt: 1, updatedAt: 8 },
  ];

  it("sortThreads: unresolved first, then most-recently-updated", () => {
    expect(sortThreads(threads).map((t) => t.id)).toEqual(["3", "1", "2"]);
  });

  it("threadsForShot filters by shot anchor", () => {
    expect(threadsForShot(threads, "s1").map((t) => t.id)).toEqual(["3", "1"]);
  });

  it("threadsInWordRange returns overlapping word anchors", () => {
    expect(threadsInWordRange(threads, 8, 12).map((t) => t.id)).toEqual(["3", "1"]);
    expect(threadsInWordRange(threads, 55, 56).map((t) => t.id)).toEqual(["2"]);
  });

  it("countThreads splits open vs resolved", () => {
    expect(countThreads(threads)).toEqual({ total: 3, open: 2, resolved: 1 });
  });
});

describe("export / import round-trip", () => {
  it("exports a book's threads and re-imports them with fresh ids", () => {
    const store = createAnnotationStore(memStore(), deps());
    store.open("book1", { shot_id: "s1" }, "Ada", "one");
    store.open("book1", { word_range: [0, 5] }, "Ada", "two");
    store.open("book2", { shot_id: "s9" }, "Ada", "other book"); // excluded

    const bundle = store.exportBook("book1");
    expect(bundle.v).toBe(1);
    expect(bundle.threads).toHaveLength(2);

    // Fresh store with a DISTINCT id prefix so re-minted ids are observably new.
    let fn = 0;
    const fresh = createAnnotationStore(memStore(), {
      clock: { now: () => 5000 },
      ids: { next: (p) => `imp_${p}${fn++}` },
    });
    const imported = fresh.importBundle(bundle);
    expect(imported).toBe(2);
    expect(fresh.forBook("book1")).toHaveLength(2);
    // ids were re-minted (carry the fresh store's prefix), not copied verbatim.
    const newIds = fresh.forBook("book1").map((t) => t.id);
    expect(newIds.every((id) => id.startsWith("imp_th"))).toBe(true);
    expect(newIds).not.toContain(bundle.threads[0].id);
    // the original comments survived the round-trip
    expect(fresh.forBook("book1").flatMap((t) => t.comments.map((c) => c.body)).sort()).toEqual([
      "one",
      "two",
    ]);
  });

  it("rejects an unknown bundle version", () => {
    const store = createAnnotationStore(memStore(), deps());
    expect(store.importBundle({ v: 2, book_id: "b", threads: [] })).toBe(0);
    expect(store.importBundle(null)).toBe(0);
    expect(store.importBundle({ v: 1, book_id: "b", threads: [] })).toBe(0);
  });
});
