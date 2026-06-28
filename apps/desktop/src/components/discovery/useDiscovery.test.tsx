import { describe, it, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useDiscovery } from "./useDiscovery";
import type { DiscoveryBook } from "../../lib/discovery/types";
import type { KeyValueStore } from "../../lib/discovery/history";

function memStore(): KeyValueStore {
  const m = new Map<string, string>();
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => void m.set(k, v) };
}

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
  };
}

const lib: DiscoveryBook[] = [
  ...Array.from({ length: 6 }, (_, i) => book({ id: `sf${i}`, genre: "SF" })),
  ...Array.from({ length: 3 }, (_, i) => book({ id: `rom${i}`, genre: "Romance" })),
];

describe("useDiscovery", () => {
  it("builds rows from a catalog (cold start → no Top Picks)", () => {
    const { result } = renderHook(() =>
      useDiscovery(lib, { store: memStore(), now: () => 0 }),
    );
    expect(result.current.profile.totalSignal).toBe(0);
    expect(result.current.rows.some((r) => r.id === "top-picks")).toBe(false);
    expect(result.current.rows.some((r) => r.id === "popular")).toBe(true);
  });

  it("records an open and surfaces personalized rows", () => {
    const { result } = renderHook(() =>
      useDiscovery(lib, { store: memStore(), now: () => 0 }),
    );
    act(() => result.current.record(book({ id: "sf0", genre: "SF" }), "open"));
    expect(result.current.profile.genres.SF).toBeGreaterThan(0);
    expect(result.current.rows.some((r) => r.id === "top-picks")).toBe(true);
    expect(result.current.recents).toContain("sf0");
  });

  it("dismiss excludes a book from recommendations", () => {
    const { result } = renderHook(() =>
      useDiscovery(lib, { store: memStore(), now: () => 0 }),
    );
    // Build a strong SF taste (several opens) so one dismiss doesn't zero the
    // whole genre; the dismiss should remove only that specific book.
    act(() => result.current.record(book({ id: "sf0", genre: "SF" }), "finish"));
    act(() => result.current.record(book({ id: "sf2", genre: "SF" }), "finish"));
    act(() => result.current.dismiss(book({ id: "sf1", genre: "SF" })));
    const topPicks = result.current.rows.find((r) => r.id === "top-picks");
    expect(topPicks).toBeDefined();
    expect(topPicks!.books.map((x) => x.id)).not.toContain("sf1");
    expect(result.current.profile.dismissed.has("sf1")).toBe(true);
  });

  it("reset clears learned taste", () => {
    const { result } = renderHook(() =>
      useDiscovery(lib, { store: memStore(), now: () => 0 }),
    );
    act(() => result.current.record(book({ id: "sf0", genre: "SF" }), "open"));
    act(() => result.current.reset());
    expect(result.current.profile.totalSignal).toBe(0);
    expect(result.current.recents).toEqual([]);
  });

  it("only pushes meaningful kinds to recents (not hovers)", () => {
    const { result } = renderHook(() =>
      useDiscovery(lib, { store: memStore(), now: () => 0 }),
    );
    act(() => result.current.record(book({ id: "sf0" }), "hover"));
    expect(result.current.recents).toEqual([]);
    act(() => result.current.record(book({ id: "sf0" }), "preview"));
    expect(result.current.recents).toContain("sf0");
  });
});
