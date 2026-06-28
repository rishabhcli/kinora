// Continue-reading model — ranks in-progress books by "most worth resuming".
// A book the reader touched recently and is mid-way through ranks above one they
// barely started long ago. Pure: takes books + (optional) last-touch timestamps.
import type { DiscoveryBook, Interaction } from "./types";
import { readingState } from "./search";
import { recencyDecay } from "./affinity";

export interface ContinueEntry {
  book: DiscoveryBook;
  /** epoch-ms of the last interaction, or null if unknown. */
  lastAt: number | null;
  score: number;
}

/** Build a map of bookId → most-recent interaction timestamp from history. */
export function lastTouchMap(history: Interaction[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const ev of history) {
    if (out[ev.bookId] === undefined || ev.at > out[ev.bookId]) out[ev.bookId] = ev.at;
  }
  return out;
}

/**
 * Rank in-progress books for the Continue Reading row. Score blends:
 *   - recency of last touch (decayed) — the dominant term
 *   - a gentle mid-progress bump (a book at 50% is more "resumable" than 95%,
 *     which is nearly done, or 2%, which is barely started)
 * Finished/unread books are excluded.
 */
export function continueReadingRanked(
  books: DiscoveryBook[],
  opts: { lastTouch?: Record<string, number>; now?: number; halfLifeDays?: number } = {},
): ContinueEntry[] {
  const now = opts.now ?? Date.now();
  const lastTouch = opts.lastTouch ?? {};

  const entries: (ContinueEntry & { idx: number })[] = [];
  books.forEach((book, idx) => {
    if (readingState(book) !== "reading") return;
    const lastAt = lastTouch[book.id] ?? null;
    const recency = lastAt === null ? 0.4 : recencyDecay(lastAt, now, opts.halfLifeDays ?? 21);
    // progress bump: triangular, peaking around 50%.
    const p = book.progress / 100;
    const midBump = 1 - Math.abs(p - 0.5) * 0.6; // 1.0 at 50%, ~0.7 at 0/100
    const score = recency * 0.75 + midBump * 0.25;
    entries.push({ book, lastAt, score, idx });
  });

  entries.sort((a, b) => b.score - a.score || a.idx - b.idx);
  return entries.map(({ book, lastAt, score }) => ({ book, lastAt, score }));
}

/** Convenience: just the ordered books (for the row component). */
export function continueReadingBooks(
  books: DiscoveryBook[],
  opts?: Parameters<typeof continueReadingRanked>[1],
): DiscoveryBook[] {
  return continueReadingRanked(books, opts).map((e) => e.book);
}

/** The single book to resume on a ⌘K "Resume reading" action, or null. */
export function nextToResume(
  books: DiscoveryBook[],
  opts?: Parameters<typeof continueReadingRanked>[1],
): DiscoveryBook | null {
  return continueReadingRanked(books, opts)[0]?.book ?? null;
}
