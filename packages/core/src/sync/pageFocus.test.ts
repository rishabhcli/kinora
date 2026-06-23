import { describe, expect, it } from "vitest";

import {
  clampZoom,
  containRect,
  fitZoom,
  focusWordAtReadingLine,
  prefetchRange,
  type RenderedPage,
  visiblePageAtLine,
  wordAtNormalisedPoint,
} from "./pageFocus";

// Two stacked 1000px-tall pages. Words are one normalised line apart (y = 0.1,
// 0.5, 0.9), each 0.04 tall, so their rendered centres on page 1 land at
// y ≈ 120, 520, 920 and on page 2 (top = 1000) at ≈ 1120, 1520, 1920.
const pages: RenderedPage[] = [
  {
    page: 1,
    top: 0,
    height: 1000,
    words: [
      { word_index: 10, bbox: [0.1, 0.1, 0.2, 0.04] },
      { word_index: 11, bbox: [0.4, 0.1, 0.2, 0.04] }, // same line as 10
      { word_index: 12, bbox: [0.1, 0.5, 0.2, 0.04] },
      { word_index: 13, bbox: [0.1, 0.9, 0.2, 0.04] },
    ],
  },
  {
    page: 2,
    top: 1000,
    height: 1000,
    words: [
      { word_index: 20, bbox: [0.1, 0.1, 0.2, 0.04] },
      { word_index: 21, bbox: [0.1, 0.5, 0.2, 0.04] },
    ],
  },
];

describe("focusWordAtReadingLine", () => {
  it("returns the word nearest the reading line", () => {
    expect(focusWordAtReadingLine(pages, 120)).toBe(10); // centre of line 1
    expect(focusWordAtReadingLine(pages, 520)).toBe(12); // centre of line 2
    expect(focusWordAtReadingLine(pages, 900)).toBe(13); // near line 3
  });

  it("breaks same-line ties toward the start of the line (smaller word_index)", () => {
    // 10 and 11 share the same vertical centre; the line start wins.
    expect(focusWordAtReadingLine(pages, 120)).toBe(10);
  });

  it("advances across a page boundary as the line moves down", () => {
    expect(focusWordAtReadingLine(pages, 1120)).toBe(20); // first word of page 2
    expect(focusWordAtReadingLine(pages, 1520)).toBe(21);
  });

  it("picks the closest even when the line sits between two words", () => {
    // Midway (320) between line1@120 and line2@520 is equidistant → earlier wins.
    expect(focusWordAtReadingLine(pages, 320)).toBe(10);
    // Just past the midpoint resolves to the lower line.
    expect(focusWordAtReadingLine(pages, 321)).toBe(12);
  });

  it("returns null when no laid-out page carries words", () => {
    const illustration: RenderedPage[] = [{ page: 5, top: 0, height: 1400, words: [] }];
    expect(focusWordAtReadingLine(illustration, 300)).toBeNull();
  });

  it("ignores unmeasured pages (height <= 0)", () => {
    const withUnmeasured: RenderedPage[] = [
      { page: 1, top: 0, height: 0, words: [{ word_index: 1, bbox: [0.1, 0.1, 0.2, 0.04] }] },
      { page: 2, top: 0, height: 1000, words: [{ word_index: 2, bbox: [0.1, 0.5, 0.2, 0.04] }] },
    ];
    expect(focusWordAtReadingLine(withUnmeasured, 520)).toBe(2);
  });
});

describe("containRect", () => {
  it("letterboxes a tall image inside a wide box (full height, centred horizontally)", () => {
    // image 1:2 (ratio 0.5) in a 400×400 box → h=400, w=200, centred.
    expect(containRect(400, 400, 0.5)).toEqual({ x: 100, y: 0, w: 200, h: 400 });
  });

  it("pillarboxes a wide image inside a tall box (full width, centred vertically)", () => {
    // image 2:1 (ratio 2) in a 400×400 box → w=400, h=200, centred.
    expect(containRect(400, 400, 2)).toEqual({ x: 0, y: 100, w: 400, h: 200 });
  });

  it("returns an empty rect for degenerate inputs", () => {
    expect(containRect(0, 100, 1)).toEqual({ x: 0, y: 0, w: 0, h: 0 });
    expect(containRect(100, 100, 0)).toEqual({ x: 0, y: 0, w: 0, h: 0 });
  });
});

describe("wordAtNormalisedPoint", () => {
  const words = [
    { word_index: 1, bbox: [0.1, 0.1, 0.2, 0.05] },
    { word_index: 2, bbox: [0.5, 0.5, 0.2, 0.05] },
  ];

  it("returns the word whose box contains the point", () => {
    expect(wordAtNormalisedPoint(words, 0.15, 0.12)).toBe(1);
    expect(wordAtNormalisedPoint(words, 0.6, 0.52)).toBe(2);
  });

  it("falls back to the nearest word centre when none contains the point", () => {
    expect(wordAtNormalisedPoint(words, 0.0, 0.0)).toBe(1);
    expect(wordAtNormalisedPoint(words, 0.9, 0.9)).toBe(2);
  });

  it("returns null when there are no words", () => {
    expect(wordAtNormalisedPoint([], 0.5, 0.5)).toBeNull();
  });
});

describe("visiblePageAtLine", () => {
  const pages: RenderedPage[] = [
    { page: 1, top: -200, height: 400, words: [] }, // spans the line at y=0..200
    { page: 2, top: 400, height: 400, words: [] },
  ];

  it("returns the page the reading line falls inside", () => {
    expect(visiblePageAtLine(pages, 100)).toBe(1); // inside page 1
    expect(visiblePageAtLine(pages, 500)).toBe(2); // inside page 2
  });

  it("returns the nearest page when the line is in the gap", () => {
    expect(visiblePageAtLine(pages, 250)).toBe(1); // gap, closer to page1 bottom (200)
    expect(visiblePageAtLine(pages, 360)).toBe(2); // gap, closer to page2 top (400)
  });

  it("returns null when nothing is laid out", () => {
    expect(visiblePageAtLine([], 100)).toBeNull();
  });
});

describe("clampZoom", () => {
  it("clamps to [0.5, 4] and guards non-finite", () => {
    expect(clampZoom(0.1)).toBe(0.5);
    expect(clampZoom(9)).toBe(4);
    expect(clampZoom(1.5)).toBe(1.5);
    expect(clampZoom(Number.NaN)).toBe(1);
  });
});

describe("fitZoom", () => {
  const pane = { width: 800, height: 600 };

  it("fit-width is the base fit (1×)", () => {
    expect(fitZoom("width", pane, 0.7, 700, 2)).toBe(1);
  });

  it("custom uses the clamped chosen zoom", () => {
    expect(fitZoom("custom", pane, 0.7, 700, 2.5)).toBe(2.5);
    expect(fitZoom("custom", pane, 0.7, 700, 99)).toBe(4);
  });

  it("fit-page sizes the page so its full height fits the pane", () => {
    // ratio 1.4 (landscape), baseWidth 700, pane height 600 → fitPageWidth =
    // (600-32)*1.4 = 795.2 → zoom = 795.2/700 ≈ 1.136.
    expect(fitZoom("page", pane, 1.4, 700, 1)).toBeCloseTo(795.2 / 700, 3);
  });

  it("falls back to 1× on degenerate geometry", () => {
    expect(fitZoom("page", { width: 0, height: 0 }, 0.7, 0, 1)).toBe(1);
  });
});

describe("prefetchRange", () => {
  it("returns nearest-first neighbours within bounds, excluding the visible page", () => {
    expect(prefetchRange(5, 200, 2)).toEqual([6, 4, 7, 3]);
  });

  it("clamps at the start of the book", () => {
    expect(prefetchRange(1, 200, 3)).toEqual([2, 3, 4]);
  });

  it("clamps at the end of the book", () => {
    expect(prefetchRange(200, 200, 3)).toEqual([199, 198, 197]);
  });
});
