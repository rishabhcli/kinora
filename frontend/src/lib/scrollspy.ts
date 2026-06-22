import type { Bbox } from "../api/types";

// Scroll-spy / focus-word computation (kinora.md §4.3). The reader's "where am
// I" signal `w` is the word nearest the *reading line* — the top third of the
// viewport, not the centre, because eyes lead the scroll. Everything here is
// pure and px-based so it can be tested against a mocked scroll position.

export const READING_LINE_FRACTION = 1 / 3;

export interface PageLayout {
  page: number;
  /** Top offset of this page within the scroll container, in px. */
  top: number;
  /** Rendered height of this page, in px. */
  height: number;
}

export interface PositionedWord {
  word_index: number;
  page: number;
  bbox: Bbox;
}

/** Absolute Y (in scroll-container coordinates) of the reading line. */
export function readingLineY(
  scrollTop: number,
  viewportHeight: number,
  fraction: number = READING_LINE_FRACTION,
): number {
  return scrollTop + viewportHeight * fraction;
}

/** Absolute Y of a word's vertical centre, or null if its page isn't laid out. */
export function wordCenterY(word: PositionedWord, pages: PageLayout[]): number | null {
  const layout = pages.find((p) => p.page === word.page);
  if (!layout) return null;
  const [, y, , h] = word.bbox;
  return layout.top + (y + h / 2) * layout.height;
}

export interface FocusWordParams {
  scrollTop: number;
  viewportHeight: number;
  pages: PageLayout[];
  /** Candidate words — typically just the words on currently-visible pages. */
  words: PositionedWord[];
  readingLineFraction?: number;
}

/**
 * The focus word `w`: the word whose rendered centre is nearest the reading
 * line. Returns null when there are no laid-out candidate words.
 */
export function computeFocusWord(params: FocusWordParams): number | null {
  const line = readingLineY(
    params.scrollTop,
    params.viewportHeight,
    params.readingLineFraction ?? READING_LINE_FRACTION,
  );
  let best: number | null = null;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const word of params.words) {
    const cy = wordCenterY(word, params.pages);
    if (cy === null) continue;
    const dist = Math.abs(cy - line);
    if (dist < bestDist) {
      bestDist = dist;
      best = word.word_index;
    }
  }
  return best;
}

/** The 0-based page index whose band contains a given absolute Y. */
export function pageAtY(y: number, pages: PageLayout[]): number | null {
  for (const layout of pages) {
    if (y >= layout.top && y < layout.top + layout.height) return layout.page;
  }
  return null;
}
