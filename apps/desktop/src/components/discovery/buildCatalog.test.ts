import { describe, it, expect } from "vitest";
import { enrich, mergeCatalog, popularityPrior } from "./buildCatalog";
import type { Book } from "../../data/books";

function book(over: Partial<Book> = {}): Book {
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

describe("enrich", () => {
  it("keeps explicit genre/era", () => {
    const b = enrich({ ...book(), genre: "SF", era: "20th" });
    expect(b.genre).toBe("SF");
    expect(b.era).toBe("20th");
  });
  it("falls back to the catalogue manifest by id", () => {
    // a known manifest id from data/catalog.ts (Moby Dick)
    const b = enrich(book({ id: "pubdom27010000000000000000000000" }));
    expect(b.genre).toBe("Adventure");
  });
});

describe("mergeCatalog", () => {
  it("dedupes by id, earlier sources win", () => {
    const a = [book({ id: "x", title: "Backend" })];
    const b = [book({ id: "x", title: "Demo" }), book({ id: "y", title: "Other" })];
    const merged = mergeCatalog(a, b);
    expect(merged.map((m) => m.id)).toEqual(["x", "y"]);
    expect(merged.find((m) => m.id === "x")!.title).toBe("Backend");
  });
  it("preserves first-seen order", () => {
    const merged = mergeCatalog([book({ id: "a" }), book({ id: "b" })], [book({ id: "c" })]);
    expect(merged.map((m) => m.id)).toEqual(["a", "b", "c"]);
  });
});

describe("popularityPrior", () => {
  it("ranks popular ids highest, descending", () => {
    const books = [book({ id: "a" }), book({ id: "b" }), book({ id: "c" })];
    const prior = popularityPrior(books, ["b", "a"]);
    expect(prior.b).toBe(1);
    expect(prior.a).toBeLessThan(prior.b);
    expect(prior.a).toBeGreaterThan(0);
  });
  it("gives non-popular books a small baseline", () => {
    const books = [book({ id: "a" }), book({ id: "z" })];
    const prior = popularityPrior(books, ["a"]);
    expect(prior.z).toBeGreaterThanOrEqual(0);
    expect(prior.z).toBeLessThan(prior.a);
  });
});
