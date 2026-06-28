import { describe, it, expect } from "vitest";
import {
  normalize,
  tokenize,
  uniqueTokens,
  levenshtein,
  isSubsequence,
  fuzzyScore,
  tokenFieldScore,
} from "./tokenize";

describe("normalize", () => {
  it("lowercases, strips accents and punctuation", () => {
    expect(normalize("Brontë!")).toBe("bronte");
    expect(normalize("Pride & Prejudice")).toBe("pride prejudice");
    expect(normalize("  The   Dune  ")).toBe("the dune");
  });
  it("returns empty for punctuation-only input", () => {
    expect(normalize("—!?")).toBe("");
    expect(normalize("")).toBe("");
  });
});

describe("tokenize / uniqueTokens", () => {
  it("splits into tokens", () => {
    expect(tokenize("Pride and Prejudice")).toEqual(["pride", "and", "prejudice"]);
    expect(tokenize("")).toEqual([]);
  });
  it("dedupes while preserving order", () => {
    expect(uniqueTokens("dune dune part two two")).toEqual(["dune", "part", "two"]);
  });
});

describe("levenshtein", () => {
  it("computes edit distance", () => {
    expect(levenshtein("kitten", "sitting")).toBe(3);
    expect(levenshtein("dune", "dune")).toBe(0);
    expect(levenshtein("", "abc")).toBe(3);
    expect(levenshtein("abc", "")).toBe(3);
  });
  it("short-circuits beyond the max budget", () => {
    // distance is large; bounded call returns max+1 quickly
    expect(levenshtein("abcdef", "uvwxyz", 1)).toBe(2);
    expect(levenshtein("cat", "car", 1)).toBe(1);
  });
});

describe("isSubsequence", () => {
  it("detects in-order character subsequences", () => {
    expect(isSubsequence("dn", "dune")).toBe(true);
    expect(isSubsequence("due", "dune")).toBe(true);
    expect(isSubsequence("nd", "dune")).toBe(false);
    expect(isSubsequence("", "anything")).toBe(true);
  });
});

describe("fuzzyScore", () => {
  it("scores exact match as 1", () => {
    expect(fuzzyScore("dune", "Dune")).toBe(1);
  });
  it("returns 0 when not all chars present in order", () => {
    expect(fuzzyScore("xyz", "Dune")).toBe(0);
    expect(fuzzyScore("end", "dune")).toBe(0); // 'e' then 'n' then 'd' not in order
  });
  it("ranks a prefix above a scattered match", () => {
    const prefix = fuzzyScore("set", "settings");
    const scattered = fuzzyScore("set", "save the elephant");
    expect(prefix).toBeGreaterThan(scattered);
    expect(prefix).toBeGreaterThan(0);
  });
  it("ranks word-boundary matches highly", () => {
    const boundary = fuzzyScore("gp", "go pricing");
    expect(boundary).toBeGreaterThan(0);
  });
  it("is bounded to [0,1]", () => {
    for (const [q, t] of [["a", "a"], ["go home", "go home"], ["x", "xxxxxxxx"]]) {
      const s = fuzzyScore(q, t);
      expect(s).toBeGreaterThanOrEqual(0);
      expect(s).toBeLessThanOrEqual(1);
    }
  });
});

describe("tokenFieldScore", () => {
  it("tiers exact > prefix > word-prefix > substring", () => {
    expect(tokenFieldScore("dune", "Dune")).toBe(1);
    expect(tokenFieldScore("du", "Dune")).toBe(0.85);
    expect(tokenFieldScore("prej", "Pride and Prejudice")).toBe(0.7);
    expect(tokenFieldScore("ride", "Pride and Prejudice")).toBe(0.55);
  });
  it("fuzzy-matches single-typo tokens of length ≥3", () => {
    expect(tokenFieldScore("dume", "Dune")).toBeGreaterThan(0); // dume→dune (1 edit)
    expect(tokenFieldScore("zz", "Dune")).toBe(0); // too short for fuzzy + no substr
  });
  it("returns 0 for empty inputs", () => {
    expect(tokenFieldScore("", "Dune")).toBe(0);
    expect(tokenFieldScore("dune", "")).toBe(0);
  });
});
