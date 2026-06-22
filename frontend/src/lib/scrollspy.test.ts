import { describe, expect, it } from "vitest";

import {
  computeFocusWord,
  type PageLayout,
  type PositionedWord,
  readingLineY,
  wordCenterY,
} from "./scrollspy";

const pages: PageLayout[] = [
  { page: 1, top: 0, height: 1000 },
  { page: 2, top: 1018, height: 1000 },
];

describe("readingLineY", () => {
  it("sits at the top third of the viewport by default", () => {
    expect(readingLineY(300, 900)).toBe(300 + 300);
  });
});

describe("wordCenterY", () => {
  it("maps a normalized box to an absolute Y on its page", () => {
    const word: PositionedWord = { word_index: 5, page: 2, bbox: [0.1, 0.5, 0.05, 0.02] };
    // page 2 top 1018 + (0.5 + 0.01) * 1000
    expect(wordCenterY(word, pages)).toBeCloseTo(1018 + 510);
  });
  it("returns null for a word whose page isn't laid out", () => {
    const word: PositionedWord = { word_index: 5, page: 9, bbox: [0, 0, 0, 0] };
    expect(wordCenterY(word, pages)).toBeNull();
  });
});

describe("computeFocusWord", () => {
  const words: PositionedWord[] = [
    { word_index: 10, page: 1, bbox: [0.1, 0.1, 0.04, 0.02] }, // center y ≈ 110
    { word_index: 11, page: 1, bbox: [0.1, 0.3, 0.04, 0.02] }, // center y ≈ 310
    { word_index: 12, page: 1, bbox: [0.1, 0.5, 0.04, 0.02] }, // center y ≈ 510
  ];

  it("picks the word nearest the reading line for a given scroll", () => {
    // scrollTop 0, viewport 900 → reading line at y=300 → nearest is word 11.
    expect(computeFocusWord({ scrollTop: 0, viewportHeight: 900, pages, words })).toBe(11);
  });

  it("advances the focus word as the reader scrolls down", () => {
    // scrollTop 210, viewport 900 → reading line at y=510 → word 12.
    expect(computeFocusWord({ scrollTop: 210, viewportHeight: 900, pages, words })).toBe(12);
  });

  it("returns null when no candidate words are laid out", () => {
    expect(computeFocusWord({ scrollTop: 0, viewportHeight: 900, pages, words: [] })).toBeNull();
  });
});
