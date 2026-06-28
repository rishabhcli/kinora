import { describe, it, expect } from "vitest";
import { memoryStore } from "./store";
import {
  TASTE_GENRES,
  TASTE_MAX_SELECTION,
  parseTaste,
  toggleGenre,
  createTasteStore,
  TASTE_STORAGE_KEY,
  type TasteGenre,
} from "./taste";

describe("parseTaste", () => {
  it("keeps only known genres, de-dupes, and caps", () => {
    expect(parseTaste(["Fantasy", "Fantasy", "Nope", 7])).toEqual(["Fantasy"]);
    expect(parseTaste("x")).toEqual([]);
    expect(parseTaste(TASTE_GENRES.slice())).toHaveLength(TASTE_MAX_SELECTION);
  });
});

describe("toggleGenre", () => {
  it("adds and removes", () => {
    expect(toggleGenre([], "Romance")).toEqual(["Romance"]);
    expect(toggleGenre(["Romance"], "Romance")).toEqual([]);
  });
  it("ignores additions beyond the cap", () => {
    const full = TASTE_GENRES.slice(0, TASTE_MAX_SELECTION) as TasteGenre[];
    const extra = TASTE_GENRES[TASTE_MAX_SELECTION];
    expect(toggleGenre(full, extra)).toEqual(full);
  });
});

describe("createTasteStore", () => {
  it("toggles, persists, rehydrates, notifies", () => {
    const backing = memoryStore();
    const store = createTasteStore(backing);
    let hits = 0;
    store.subscribe(() => hits++);

    store.toggle("Science Fiction");
    expect(store.get()).toEqual(["Science Fiction"]);
    expect(hits).toBe(1);
    expect(backing.getItem(TASTE_STORAGE_KEY)).toContain("Science Fiction");

    expect(createTasteStore(backing).get()).toEqual(["Science Fiction"]);

    store.set(["Horror", "Bogus" as never, "Horror" as never]);
    expect(store.get()).toEqual(["Horror"]);
  });
});
