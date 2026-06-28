import { describe, it, expect } from "vitest";
import type { DiscoveryBook } from "./types";
import { expandQuery, bookBag, buildDocFreq, semanticSearch } from "./semantic";

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
    genre: over.genre,
    era: over.era,
  };
}

const lib: DiscoveryBook[] = [
  book({ id: "dune", title: "Dune", author: "Frank Herbert", genre: "Science Fiction", era: "20th century" }),
  book({ id: "neuro", title: "Neuromancer", author: "William Gibson", genre: "Science Fiction", era: "20th century" }),
  book({ id: "pride", title: "Pride and Prejudice", author: "Jane Austen", genre: "Romance", era: "19th century" }),
  book({ id: "moby", title: "Moby Dick", author: "Herman Melville", genre: "Adventure", era: "19th century" }),
];

describe("expandQuery", () => {
  it("adds synonyms for known terms", () => {
    expect(expandQuery(["space"])).toEqual(expect.arrayContaining(["space", "science", "fiction"]));
    expect(expandQuery(["love"])).toContain("romance");
  });
  it("leaves unknown tokens untouched", () => {
    expect(expandQuery(["dune"])).toEqual(["dune"]);
  });
});

describe("bookBag", () => {
  it("tokenizes title/author/genre/era", () => {
    const bag = bookBag(lib[0]);
    expect(bag.has("dune")).toBe(true);
    expect(bag.has("science")).toBe(true);
    expect(bag.has("herbert")).toBe(true);
  });
});

describe("buildDocFreq", () => {
  it("counts how many books contain each token", () => {
    const df = buildDocFreq(lib);
    expect(df.get("science")).toBe(2); // dune + neuromancer
    expect(df.get("dune")).toBe(1);
  });
});

describe("semanticSearch", () => {
  it("surfaces SF books for a 'space' query via synonym expansion", () => {
    const r = semanticSearch(lib, "space");
    const ids = r.map((h) => h.book.id);
    expect(ids).toContain("dune");
    expect(ids).toContain("neuro");
    expect(ids).not.toContain("pride");
  });

  it("surfaces romance for 'victorian love'", () => {
    const r = semanticSearch(lib, "victorian love");
    expect(r[0].book.id).toBe("pride");
  });

  it("ranks a rarer token match above a common one", () => {
    // "dune" is rare (df 1) vs "fiction" (df 2); a direct title hit should top.
    const r = semanticSearch(lib, "dune");
    expect(r[0].book.id).toBe("dune");
  });

  it("returns nothing for an empty or unmatched query", () => {
    expect(semanticSearch(lib, "")).toEqual([]);
    expect(semanticSearch(lib, "qwerty")).toEqual([]);
  });

  it("honors the limit", () => {
    expect(semanticSearch(lib, "science fiction", { limit: 1 }).length).toBe(1);
  });
});
