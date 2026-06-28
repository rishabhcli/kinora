import { describe, it, expect } from "vitest";
import type { DiscoveryBook, TasteProfile } from "./types";
import { scoreCandidate, recommend, similarTo, DEFAULT_WEIGHTS } from "./scoring";

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

function profile(over: Partial<TasteProfile> = {}): TasteProfile {
  return {
    genres: over.genres ?? {},
    eras: over.eras ?? {},
    authors: over.authors ?? {},
    dismissed: over.dismissed ?? new Set<string>(),
    totalSignal: over.totalSignal ?? 0,
  };
}

describe("scoreCandidate", () => {
  it("rewards a matching genre", () => {
    const s = scoreCandidate(book({ genre: "SF" }), profile({ genres: { SF: 10 } }));
    expect(s).not.toBeNull();
    expect(s!.parts.genre).toBeGreaterThan(0);
    expect(s!.reason).toMatch(/SF/);
    expect(s!.basis).toBe("genre");
  });
  it("weights author above genre", () => {
    const authorMatch = scoreCandidate(book({ author: "Herbert" }), profile({ authors: { Herbert: 10 } }));
    const genreMatch = scoreCandidate(book({ genre: "SF" }), profile({ genres: { SF: 10 } }));
    expect(authorMatch!.score).toBeGreaterThan(genreMatch!.score);
    expect(authorMatch!.basis).toBe("author");
  });
  it("excludes dismissed books", () => {
    expect(scoreCandidate(book({ id: "x" }), profile({ dismissed: new Set(["x"]) }))).toBeNull();
  });
  it("excludes finished books", () => {
    expect(scoreCandidate(book({ progress: 100, genre: "SF" }), profile({ genres: { SF: 5 } }))).toBeNull();
  });
  it("gives a novelty boost to new unread books with no taste match", () => {
    const s = scoreCandidate(book({ isNew: true, progress: 0 }), profile());
    expect(s!.parts.novelty).toBeGreaterThan(0);
    expect(s!.basis).toBe("new");
  });
  it("respects custom weights", () => {
    const low = scoreCandidate(book({ genre: "SF" }), profile({ genres: { SF: 10 } }), {
      weights: { ...DEFAULT_WEIGHTS, genre: 0 },
    });
    expect(low!.parts.genre).toBe(0);
  });
});

describe("recommend", () => {
  const lib: DiscoveryBook[] = [
    book({ id: "a", author: "Herbert", genre: "SF" }),
    book({ id: "b", genre: "SF" }),
    book({ id: "c", genre: "Romance" }),
    book({ id: "d", progress: 100, genre: "SF" }), // finished → excluded
  ];

  it("ranks the strongest match first and excludes finished/zero-score", () => {
    const recs = recommend(lib, profile({ authors: { Herbert: 8 }, genres: { SF: 8 } }));
    expect(recs[0].book.id).toBe("a"); // author + genre
    expect(recs.map((r) => r.book.id)).toContain("b");
    expect(recs.map((r) => r.book.id)).not.toContain("c"); // no romance signal
    expect(recs.map((r) => r.book.id)).not.toContain("d"); // finished
  });

  it("applies a popularity prior", () => {
    const recs = recommend(lib, profile(), { popularity: { c: 1 } });
    expect(recs[0].book.id).toBe("c");
  });

  it("honors the limit", () => {
    const recs = recommend(lib, profile({ genres: { SF: 8, Romance: 8 } }), { limit: 1 });
    expect(recs.length).toBe(1);
  });

  it("is stable on ties (catalog order)", () => {
    const recs = recommend(
      [book({ id: "x", genre: "SF" }), book({ id: "y", genre: "SF" })],
      profile({ genres: { SF: 8 } }),
    );
    expect(recs.map((r) => r.book.id)).toEqual(["x", "y"]);
  });
});

describe("similarTo", () => {
  const lib: DiscoveryBook[] = [
    book({ id: "seed", author: "Herbert", genre: "SF", era: "20th" }),
    book({ id: "sameAuthor", author: "Herbert", genre: "SF", era: "20th" }),
    book({ id: "sameGenre", author: "Gibson", genre: "SF", era: "20th" }),
    book({ id: "sameEra", author: "Austen", genre: "Romance", era: "20th" }),
    book({ id: "none", author: "Tolkien", genre: "Fantasy", era: "20th-fantasy" }),
  ];
  it("ranks by overlap: author > genre > era and excludes the seed", () => {
    const r = similarTo(lib[0], lib).map((b) => b.id);
    expect(r[0]).toBe("sameAuthor");
    expect(r).not.toContain("seed");
    expect(r.indexOf("sameGenre")).toBeLessThan(r.indexOf("sameEra"));
  });
  it("omits books with no overlap", () => {
    expect(similarTo(lib[0], lib)).not.toContainEqual(expect.objectContaining({ id: "none" }));
  });
});
