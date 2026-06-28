import { describe, it, expect } from "vitest";
import type { Interaction } from "./types";
import {
  KIND_WEIGHTS,
  recencyDecay,
  buildProfile,
  isColdStart,
  topAffinities,
  favoriteGenre,
} from "./affinity";

const DAY = 86_400_000;

function ev(over: Partial<Interaction> & Pick<Interaction, "bookId" | "kind">): Interaction {
  return { at: 0, ...over };
}

describe("recencyDecay", () => {
  it("is 1 at age 0 and ~0.5 at one half-life", () => {
    expect(recencyDecay(1000, 1000)).toBe(1);
    expect(recencyDecay(0, 14 * DAY, 14)).toBeCloseTo(0.5, 5);
  });
  it("never goes negative or above 1", () => {
    expect(recencyDecay(0, 100 * DAY)).toBeGreaterThan(0);
    expect(recencyDecay(0, 100 * DAY)).toBeLessThan(1);
  });
});

describe("buildProfile", () => {
  it("accumulates weighted, decayed affinity per dimension", () => {
    const now = 0;
    const hist: Interaction[] = [
      ev({ bookId: "a", kind: "open", genre: "SF", era: "20th", author: "Herbert", at: 0 }),
      ev({ bookId: "b", kind: "finish", genre: "SF", era: "20th", author: "Gibson", at: 0 }),
    ];
    const p = buildProfile(hist, { now });
    expect(p.genres.SF).toBeCloseTo(KIND_WEIGHTS.open + KIND_WEIGHTS.finish, 5);
    expect(p.eras["20th"]).toBeCloseTo(KIND_WEIGHTS.open + KIND_WEIGHTS.finish, 5);
    expect(p.authors.Herbert).toBeCloseTo(KIND_WEIGHTS.open, 5);
  });

  it("decays older signals", () => {
    const fresh = buildProfile([ev({ bookId: "a", kind: "open", genre: "SF", at: 0 })], { now: 0 });
    const old = buildProfile([ev({ bookId: "a", kind: "open", genre: "SF", at: 0 })], {
      now: 14 * DAY,
      halfLifeDays: 14,
    });
    expect(old.genres.SF).toBeCloseTo(fresh.genres.SF / 2, 4);
  });

  it("records dismissals and applies negative weight", () => {
    const p = buildProfile([ev({ bookId: "x", kind: "dismiss", genre: "Romance", at: 0 })], { now: 0 });
    expect(p.dismissed.has("x")).toBe(true);
    expect(p.genres.Romance).toBeLessThan(0);
    expect(p.totalSignal).toBe(0); // negative signal doesn't add to positive total
  });

  it("ignores missing dimensions", () => {
    const p = buildProfile([ev({ bookId: "a", kind: "open", at: 0 })], { now: 0 });
    expect(Object.keys(p.genres)).toEqual([]);
    expect(p.totalSignal).toBeGreaterThan(0);
  });
});

describe("isColdStart", () => {
  it("is true with no positive signal", () => {
    expect(isColdStart(buildProfile([], { now: 0 }))).toBe(true);
  });
  it("is false once the reader has opened something", () => {
    const p = buildProfile([ev({ bookId: "a", kind: "open", genre: "SF", at: 0 })], { now: 0 });
    expect(isColdStart(p)).toBe(false);
  });
});

describe("topAffinities / favoriteGenre", () => {
  it("returns top-N positive keys descending", () => {
    expect(topAffinities({ a: 5, b: 1, c: 3, d: -2 }, 2)).toEqual(["a", "c"]);
  });
  it("picks the single strongest genre", () => {
    const p = buildProfile(
      [
        ev({ bookId: "a", kind: "open", genre: "SF", at: 0 }),
        ev({ bookId: "b", kind: "hover", genre: "Romance", at: 0 }),
      ],
      { now: 0 },
    );
    expect(favoriteGenre(p)).toBe("SF");
  });
  it("returns null with no genre affinity", () => {
    expect(favoriteGenre(buildProfile([], { now: 0 }))).toBeNull();
  });
});
