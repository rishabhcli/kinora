import { describe, it, expect } from "vitest";
import type { DiscoveryBook } from "./types";
import {
  deriveFacets,
  toggleFacetValue,
  hasActiveFacets,
  activeFacetCount,
  stateKeyFromLabel,
} from "./facets";

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
  book({ id: "a", author: "Melville", genre: "Adventure", era: "19th", progress: 40 }),
  book({ id: "b", author: "Austen", genre: "Romance", era: "19th", progress: 100 }),
  book({ id: "c", author: "Herbert", genre: "SF", era: "20th", progress: 0 }),
  book({ id: "d", author: "Gibson", genre: "SF", era: "20th", progress: 0 }),
];

describe("deriveFacets", () => {
  it("lists genres with counts, sorted by count then name", () => {
    const facets = deriveFacets(lib);
    const genre = facets.find((f) => f.key === "genre")!;
    expect(genre.values[0]).toEqual({ value: "SF", count: 2 });
    expect(genre.values.map((v) => v.value)).toContain("Romance");
  });

  it("computes a facet's own counts ignoring its own selection", () => {
    // Selecting genre=SF should not collapse the genre facet to only SF.
    const facets = deriveFacets(lib, { genre: ["SF"] });
    const genre = facets.find((f) => f.key === "genre")!;
    expect(genre.values.map((v) => v.value).sort()).toEqual(["Adventure", "Romance", "SF"]);
  });

  it("narrows OTHER facets by the active selection", () => {
    // genre=SF → era facet should only show 20th (count 2).
    const facets = deriveFacets(lib, { genre: ["SF"] });
    const era = facets.find((f) => f.key === "era")!;
    expect(era.values).toEqual([{ value: "20th", count: 2 }]);
  });

  it("derives reading-state counts with human labels", () => {
    const facets = deriveFacets(lib);
    const state = facets.find((f) => f.key === "state")!;
    const labels = state.values.map((v) => v.value);
    expect(labels).toContain("In progress");
    expect(labels).toContain("Finished");
    expect(labels).toContain("Not started");
  });

  it("omits facets with no values", () => {
    const facets = deriveFacets([book({ id: "x" })]); // no genre/era
    expect(facets.find((f) => f.key === "genre")).toBeUndefined();
  });
});

describe("toggleFacetValue", () => {
  it("adds and removes immutably", () => {
    expect(toggleFacetValue(undefined, "SF")).toEqual(["SF"]);
    expect(toggleFacetValue(["SF"], "Romance").sort()).toEqual(["Romance", "SF"]);
    expect(toggleFacetValue(["SF", "Romance"], "SF")).toEqual(["Romance"]);
  });
});

describe("hasActiveFacets / activeFacetCount", () => {
  it("detects active state", () => {
    expect(hasActiveFacets({})).toBe(false);
    expect(hasActiveFacets({ text: "  " })).toBe(false);
    expect(hasActiveFacets({ text: "x" })).toBe(true);
    expect(hasActiveFacets({ genre: ["SF"] })).toBe(true);
  });
  it("counts active facet chips (excludes text)", () => {
    expect(activeFacetCount({ genre: ["SF", "Romance"], state: ["unread"] })).toBe(3);
    expect(activeFacetCount({ text: "x" })).toBe(0);
  });
});

describe("stateKeyFromLabel", () => {
  it("maps labels back to keys", () => {
    expect(stateKeyFromLabel("In progress")).toBe("reading");
    expect(stateKeyFromLabel("Finished")).toBe("finished");
    expect(stateKeyFromLabel("Not started")).toBe("unread");
    expect(stateKeyFromLabel("nope")).toBeNull();
  });
});
