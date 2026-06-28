import { describe, it, expect } from "vitest";
import type { DiscoveryBook, Interaction, TasteProfile } from "./types";
import { buildRows } from "./rows";

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

// A catalog large enough that rows clear the min-size threshold (3).
function genreSet(genre: string, n: number, opts: Partial<DiscoveryBook> = {}): DiscoveryBook[] {
  return Array.from({ length: n }, (_, i) => book({ id: `${genre}-${i}`, genre, ...opts }));
}

describe("buildRows", () => {
  it("puts Continue Reading first when there are in-progress books", () => {
    const lib = [
      ...genreSet("SF", 4, { progress: 40 }),
      ...genreSet("Romance", 4),
    ];
    const rows = buildRows(lib, { profile: profile(), now: 0, popularity: {} });
    expect(rows[0].id).toBe("continue");
    expect(rows[0].books.length).toBeGreaterThanOrEqual(3);
  });

  it("falls back to Popular on a cold start (no Top Picks row)", () => {
    const lib = genreSet("SF", 6);
    const rows = buildRows(lib, { profile: profile(), now: 0 });
    expect(rows.map((r) => r.id)).not.toContain("top-picks");
    expect(rows.map((r) => r.id)).toContain("popular");
  });

  it("emits Top Picks once the reader has taste", () => {
    const lib = genreSet("SF", 8);
    const rows = buildRows(lib, {
      profile: profile({ genres: { SF: 10 }, totalSignal: 10 }),
      now: 0,
    });
    expect(rows.map((r) => r.id)).toContain("top-picks");
  });

  it("adds a 'More <genre>' row for top genres", () => {
    const lib = [...genreSet("SF", 8), ...genreSet("Romance", 8)];
    const rows = buildRows(lib, {
      profile: profile({ genres: { SF: 12, Romance: 6 }, totalSignal: 18 }),
      now: 0,
    });
    const genreRows = rows.filter((r) => r.kind === "genre");
    expect(genreRows.some((r) => r.title === "More SF")).toBe(true);
  });

  it("keeps the generic New/Popular tail distinct from earlier rows", () => {
    // Thematic genre rows may overlap Top Picks (Netflix-style), but the
    // generic New/Popular rows must not repeat anything already shown.
    const lib = [...genreSet("SF", 8), ...genreSet("Romance", 8, { isNew: true })];
    const rows = buildRows(lib, {
      profile: profile({ genres: { SF: 12 }, totalSignal: 12 }),
      now: 0,
    });
    const earlierIds = new Set<string>();
    const tailIds: string[] = [];
    for (const row of rows) {
      if (row.kind === "new" || row.kind === "popular") {
        tailIds.push(...row.books.map((b) => b.id));
      } else if (row.kind !== "rediscover") {
        for (const b of row.books) earlierIds.add(b.id);
      }
    }
    // No tail book was already shown in Continue/TopPicks/Genre rows.
    expect(tailIds.filter((id) => earlierIds.has(id))).toEqual([]);
    // And the tail itself has no internal duplicates.
    expect(new Set(tailIds).size).toBe(tailIds.length);
  });

  it("drops rows below the min size", () => {
    const lib = [book({ id: "a", genre: "SF" })]; // only one book
    const rows = buildRows(lib, { profile: profile(), now: 0, minRowSize: 3 });
    expect(rows).toEqual([]);
  });

  it("caps rows at maxRowSize", () => {
    const lib = genreSet("SF", 30);
    const rows = buildRows(lib, { profile: profile(), now: 0, maxRowSize: 10 });
    expect(rows.every((r) => r.books.length <= 10)).toBe(true);
  });

  it("surfaces finished books in a Watch Again row", () => {
    const lib = [...genreSet("SF", 4, { progress: 100 }), ...genreSet("Romance", 4)];
    const rows = buildRows(lib, { profile: profile(), now: 0 });
    const rediscover = rows.find((r) => r.kind === "rediscover");
    expect(rediscover).toBeDefined();
    expect(rediscover!.books.every((b) => b.progress === 100)).toBe(true);
  });
});
