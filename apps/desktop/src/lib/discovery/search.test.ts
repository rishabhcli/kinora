import { describe, it, expect } from "vitest";
import type { DiscoveryBook } from "./types";
import {
  readingState,
  scoreBook,
  applyFacetConstraints,
  search,
  suggest,
  didYouMean,
} from "./search";

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
    live: over.live,
    genre: over.genre,
    era: over.era,
    isNew: over.isNew,
  };
}

const lib: DiscoveryBook[] = [
  book({ id: "a", title: "Moby Dick", author: "Herman Melville", genre: "Adventure", era: "19th century", progress: 40 }),
  book({ id: "b", title: "Pride and Prejudice", author: "Jane Austen", genre: "Romance", era: "19th century", progress: 100 }),
  book({ id: "c", title: "Dune", author: "Frank Herbert", genre: "Science Fiction", era: "20th century", progress: 0 }),
  book({ id: "d", title: "Neuromancer", author: "William Gibson", genre: "Science Fiction", era: "20th century", progress: 0 }),
];

describe("readingState", () => {
  it("buckets by progress", () => {
    expect(readingState(book({ progress: 0 }))).toBe("unread");
    expect(readingState(book({ progress: 50 }))).toBe("reading");
    expect(readingState(book({ progress: 100 }))).toBe("finished");
  });
});

describe("scoreBook", () => {
  it("scores a title match high", () => {
    const s = scoreBook(lib[2], "dune");
    expect(s).not.toBeNull();
    expect(s!.score).toBeGreaterThan(0.8);
    expect(s!.matchedFields).toContain("title");
  });
  it("matches author tokens", () => {
    const s = scoreBook(lib[0], "melville");
    expect(s!.matchedFields).toContain("author");
  });
  it("returns null when any token matches nothing (AND semantics)", () => {
    expect(scoreBook(lib[2], "dune zzzz")).toBeNull();
  });
  it("matches across fields (genre token)", () => {
    const s = scoreBook(lib[2], "science");
    expect(s!.matchedFields).toContain("genre");
  });
});

describe("applyFacetConstraints", () => {
  it("filters by genre", () => {
    const r = applyFacetConstraints(lib, { genre: ["Science Fiction"] });
    expect(r.map((b) => b.id).sort()).toEqual(["c", "d"]);
  });
  it("filters by reading state", () => {
    expect(applyFacetConstraints(lib, { state: ["finished"] }).map((b) => b.id)).toEqual(["b"]);
    expect(applyFacetConstraints(lib, { state: ["reading"] }).map((b) => b.id)).toEqual(["a"]);
  });
  it("AND-combines facets", () => {
    const r = applyFacetConstraints(lib, { genre: ["Science Fiction"], state: ["unread"] });
    expect(r.map((b) => b.id).sort()).toEqual(["c", "d"]);
  });
  it("is case/diacritic insensitive on facet values", () => {
    expect(applyFacetConstraints(lib, { genre: ["science fiction"] }).length).toBe(2);
  });
});

describe("search", () => {
  it("returns facet-filtered books in order when no text", () => {
    const r = search(lib, { genre: ["Science Fiction"] });
    expect(r.map((h) => h.book.id)).toEqual(["c", "d"]);
    expect(r.every((h) => h.score === 0)).toBe(true);
  });
  it("ranks the best text match first", () => {
    const r = search(lib, { text: "dune" });
    expect(r[0].book.id).toBe("c");
  });
  it("combines text + facets", () => {
    const r = search(lib, { text: "herbert", genre: ["Science Fiction"] });
    expect(r.map((h) => h.book.id)).toEqual(["c"]);
  });
  it("is stable on ties (catalog order)", () => {
    // Both SF books share the genre token; tie should keep catalog order c,d.
    const r = search(lib, { text: "fiction" });
    expect(r.map((h) => h.book.id)).toEqual(["c", "d"]);
  });
  it("drops non-matching books", () => {
    expect(search(lib, { text: "zebra" })).toEqual([]);
  });
});

describe("suggest", () => {
  it("returns up to N title/author hits", () => {
    const r = suggest(lib, "her", 2); // Herman, Herbert
    expect(r.length).toBeLessThanOrEqual(2);
    expect(r.length).toBeGreaterThan(0);
  });
  it("returns nothing for empty query", () => {
    expect(suggest(lib, "  ")).toEqual([]);
  });
});

describe("didYouMean", () => {
  it("suggests a close title for a typo", () => {
    expect(didYouMean(lib, "dume")).toBe("Dune");
  });
  it("returns null when nothing is close", () => {
    expect(didYouMean(lib, "qwerty")).toBeNull();
  });
});
