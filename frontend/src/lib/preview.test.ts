import { describe, expect, it } from "vitest";

import { beats, clampIndex, tokenize, zoneForOffset } from "./preview";

describe("tokenize", () => {
  it("flattens every beat into words tagged with their beat id", () => {
    const tokens = tokenize();
    expect(tokens.length).toBeGreaterThan(0);
    // Every beat is represented.
    const seen = new Set(tokens.map((t) => t.beat));
    expect(seen.size).toBe(beats.length);
    // Words never contain whitespace.
    expect(tokens.every((t) => t.word.length > 0 && !/\s/.test(t.word))).toBe(true);
    // Token count equals the sum of words across beats.
    const expected = beats.reduce((n, b) => n + b.text.split(/\s+/).filter(Boolean).length, 0);
    expect(tokens.length).toBe(expected);
  });
});

describe("zoneForOffset", () => {
  it("classifies shots by distance ahead of the playhead", () => {
    expect(zoneForOffset(-1)).toBe("played");
    expect(zoneForOffset(0)).toBe("playing");
    expect(zoneForOffset(1)).toBe("committed");
    expect(zoneForOffset(2)).toBe("speculative");
    expect(zoneForOffset(3)).toBe("cold");
    expect(zoneForOffset(9)).toBe("cold");
  });
});

describe("clampIndex", () => {
  it("keeps an index within [0, length)", () => {
    expect(clampIndex(-5, 10)).toBe(0);
    expect(clampIndex(4, 10)).toBe(4);
    expect(clampIndex(99, 10)).toBe(9);
    expect(clampIndex(3, 0)).toBe(0);
  });
});
