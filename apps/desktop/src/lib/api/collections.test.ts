import { describe, it, expect } from "vitest";
import type { LibraryBook } from "./library";
import {
  applyFacets,
  summarizeFacets,
  sortBySpecs,
  readingState,
  evaluateCollection,
  defaultCollections,
  orderedCollections,
  createCollectionStore,
  isBuiltinCollection,
  type KeyValueStore,
  type SmartCollection,
} from "./collections";

function book(over: Partial<LibraryBook> = {}): LibraryBook {
  return {
    id: over.id ?? "id",
    title: over.title ?? "Title",
    author: over.author ?? "Author",
    progress: over.progress ?? 0,
    coverColor: "#000",
    coverGradient: "g",
    coverImage: "",
    textColor: "#fff",
    spineColor: "#000",
    live: over.live,
    genre: over.genre,
    era: over.era,
    isNew: over.isNew,
  };
}

const memStore = (seed?: Record<string, string>): KeyValueStore => {
  const m = new Map(Object.entries(seed ?? {}));
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v) };
};

const lib: LibraryBook[] = [
  book({ id: "a", title: "Moby Dick", author: "Melville", genre: "Adventure", era: "19th century", progress: 40, live: true }),
  book({ id: "b", title: "Pride and Prejudice", author: "Austen", genre: "Romance", era: "19th century", progress: 100 }),
  book({ id: "c", title: "Neuromancer", author: "Gibson", genre: "Science Fiction", era: "20th century", progress: 0, live: true }),
  book({ id: "d", title: "Dune", author: "Herbert", genre: "Science Fiction", era: "20th century", progress: 0 }),
];

describe("readingState", () => {
  it("buckets by progress", () => {
    expect(readingState(book({ progress: 0 }))).toBe("unread");
    expect(readingState(book({ progress: 50 }))).toBe("in_progress");
    expect(readingState(book({ progress: 100 }))).toBe("finished");
  });
});

describe("applyFacets", () => {
  it("text matches title, author, genre and is AND-of-tokens", () => {
    expect(applyFacets(lib, { text: "moby" }).map((b) => b.id)).toEqual(["a"]);
    expect(applyFacets(lib, { text: "science herbert" }).map((b) => b.id)).toEqual(["d"]);
    expect(applyFacets(lib, { text: "austen" }).map((b) => b.id)).toEqual(["b"]);
  });

  it("ORs within a facet kind, ANDs across kinds", () => {
    const r = applyFacets(lib, { genres: ["Science Fiction", "Adventure"], states: ["unread"] });
    expect(r.map((b) => b.id).sort()).toEqual(["c", "d"]);
  });

  it("liveOnly keeps only backend-driven books", () => {
    expect(applyFacets(lib, { liveOnly: true }).map((b) => b.id).sort()).toEqual(["a", "c"]);
  });

  it("empty query returns everything", () => {
    expect(applyFacets(lib, {})).toHaveLength(4);
  });
});

describe("summarizeFacets", () => {
  it("counts genres, eras, and states", () => {
    const s = summarizeFacets(lib);
    expect(s.genres.find((g) => g.value === "Science Fiction")?.count).toBe(2);
    expect(s.eras.find((e) => e.value === "19th century")?.count).toBe(2);
    expect(s.states.find((x) => x.value === "Unread")?.count).toBe(2);
  });
});

describe("sortBySpecs", () => {
  it("is a stable multi-key sort", () => {
    // genre asc: Adventure(a) < Romance(b) < Science Fiction(c,d);
    // within SF, title asc: Dune(d) < Neuromancer(c).
    const r = sortBySpecs(lib, [
      { field: "genre", dir: "asc" },
      { field: "title", dir: "asc" },
    ]);
    expect(r.map((b) => b.id)).toEqual(["a", "b", "d", "c"]);
  });

  it("respects direction", () => {
    expect(sortBySpecs(lib, [{ field: "progress", dir: "desc" }])[0].id).toBe("b");
    expect(sortBySpecs(lib, [{ field: "progress", dir: "asc" }])[0].progress).toBe(0);
  });
});

describe("smart collections", () => {
  it("evaluates a collection against the library", () => {
    const c: SmartCollection = {
      id: "x",
      name: "SF unread",
      query: { genres: ["Science Fiction"], states: ["unread"] },
      sort: [{ field: "title", dir: "asc" }],
      createdAt: 1,
    };
    expect(evaluateCollection(lib, c).map((b) => b.id)).toEqual(["d", "c"]);
  });

  it("built-in 'Continue Reading' surfaces only in-progress books", () => {
    const cr = defaultCollections().find((c) => c.id === "builtin:in-progress")!;
    expect(evaluateCollection(lib, cr).map((b) => b.id)).toEqual(["a"]);
  });

  it("orders pinned collections first then by createdAt", () => {
    const list = orderedCollections([
      { id: "u1", name: "u1", query: {}, sort: [], createdAt: 100 },
      { id: "p1", name: "p1", query: {}, sort: [], createdAt: 200, pinned: true },
    ]);
    expect(list[0].id).toBe("p1");
  });
});

describe("collection store", () => {
  it("always includes built-ins, persists user collections, notifies", () => {
    const backing = memStore();
    const store = createCollectionStore(backing);
    expect(store.list().every(isBuiltinCollection)).toBe(true);

    let notified = 0;
    const off = store.subscribe(() => notified++);

    store.upsert({ id: "mine", name: "Mine", query: { liveOnly: true }, sort: [], createdAt: 5 });
    expect(store.userCollections().map((c) => c.id)).toEqual(["mine"]);
    expect(notified).toBe(1);

    // a fresh store over the same backing rehydrates the user collection
    expect(createCollectionStore(backing).userCollections()).toHaveLength(1);

    store.remove("mine");
    expect(store.userCollections()).toHaveLength(0);
    expect(notified).toBe(2);

    off();
    store.upsert({ id: "again", name: "x", query: {}, sort: [], createdAt: 6 });
    expect(notified).toBe(2); // unsubscribed
  });

  it("refuses to upsert a built-in id", () => {
    const store = createCollectionStore(memStore());
    expect(() => store.upsert({ id: "builtin:live", name: "x", query: {}, sort: [] })).toThrow();
  });

  it("drops malformed persisted rows", () => {
    const backing = memStore({ "kinora.collections.v1": '[{"id":"ok","name":"ok","query":{}},{"bad":true}]' });
    expect(createCollectionStore(backing).userCollections().map((c) => c.id)).toEqual(["ok"]);
  });
});
