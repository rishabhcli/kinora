import { describe, it, expect } from "vitest";
import type { DiscoveryBook, Interaction } from "./types";
import {
  lastTouchMap,
  continueReadingRanked,
  continueReadingBooks,
  nextToResume,
} from "./continueReading";

const DAY = 86_400_000;

function book(over: Partial<DiscoveryBook> = {}): DiscoveryBook {
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
  };
}

describe("lastTouchMap", () => {
  it("keeps the latest timestamp per book", () => {
    const hist: Interaction[] = [
      { bookId: "a", kind: "view", at: 1 },
      { bookId: "a", kind: "open", at: 5 },
      { bookId: "b", kind: "view", at: 3 },
    ];
    expect(lastTouchMap(hist)).toEqual({ a: 5, b: 3 });
  });
});

describe("continueReadingRanked", () => {
  const lib: DiscoveryBook[] = [
    book({ id: "fresh", progress: 50 }),
    book({ id: "stale", progress: 50 }),
    book({ id: "done", progress: 100 }),
    book({ id: "unread", progress: 0 }),
  ];

  it("only includes in-progress books", () => {
    const r = continueReadingRanked(lib, { now: 0 });
    expect(r.map((e) => e.book.id).sort()).toEqual(["fresh", "stale"]);
  });

  it("ranks the recently-touched book first", () => {
    const r = continueReadingRanked(lib, {
      now: 10 * DAY,
      lastTouch: { fresh: 10 * DAY, stale: 0 },
    });
    expect(r[0].book.id).toBe("fresh");
  });

  it("prefers mid-progress over near-finished when recency is equal", () => {
    const books = [book({ id: "mid", progress: 50 }), book({ id: "almost", progress: 95 })];
    const r = continueReadingRanked(books, { now: 0, lastTouch: { mid: 0, almost: 0 } });
    expect(r[0].book.id).toBe("mid");
  });

  it("exposes the lastAt timestamp", () => {
    const r = continueReadingRanked(lib, { now: 0, lastTouch: { fresh: 123 } });
    expect(r.find((e) => e.book.id === "fresh")!.lastAt).toBe(123);
    expect(r.find((e) => e.book.id === "stale")!.lastAt).toBeNull();
  });
});

describe("continueReadingBooks / nextToResume", () => {
  const lib = [book({ id: "a", progress: 30 }), book({ id: "b", progress: 60 })];
  it("returns just the ordered books (recency dominates)", () => {
    // `a` touched today, `b` touched 30 days ago → `a` ranks first despite
    // `b`'s more central progress.
    const r = continueReadingBooks(lib, { now: 30 * DAY, lastTouch: { a: 30 * DAY, b: 0 } });
    expect(r.map((b) => b.id)).toEqual(["a", "b"]);
  });
  it("nextToResume returns the top book or null", () => {
    expect(nextToResume(lib, { now: 0, lastTouch: { b: 10, a: 0 } })!.id).toBe("b");
    expect(nextToResume([book({ progress: 0 })], { now: 0 })).toBeNull();
  });
});
