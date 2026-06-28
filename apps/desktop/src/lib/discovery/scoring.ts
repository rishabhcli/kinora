// Recommendation scoring — given a taste profile, score each candidate book and
// attach a human-readable reason. Pure + deterministic. The score blends:
//   genre affinity · author affinity · era affinity · popularity prior · novelty
// Dismissed and already-finished books are excluded by default.
import type { DiscoveryBook, Recommendation, TasteProfile } from "./types";
import { readingState } from "./search";

export interface ScoreWeights {
  genre: number;
  author: number;
  era: number;
  popularity: number;
  novelty: number;
}

export const DEFAULT_WEIGHTS: ScoreWeights = {
  genre: 1,
  author: 1.4, // a matching author is the strongest single signal
  era: 0.4,
  popularity: 0.3,
  novelty: 0.2,
};

/** Squash an unbounded affinity weight into [0,1] so one dimension can't
 *  dominate. Logistic-ish; 0 → 0, large → ~1. */
function squash(x: number): number {
  if (x <= 0) return 0;
  return x / (x + 3);
}

export interface ScoredBook extends Recommendation {
  /** Per-dimension contributions, exposed for explanations + debugging. */
  parts: { genre: number; author: number; era: number; popularity: number; novelty: number };
}

/**
 * Score a single candidate book against a profile. `popularityRank` is an
 * optional 0..1 prior (1 = most popular) so editorial/popular ordering can
 * nudge ties. Returns null when the book is dismissed or finished (excluded).
 */
export function scoreCandidate(
  book: DiscoveryBook,
  profile: TasteProfile,
  opts: { weights?: ScoreWeights; popularityRank?: number } = {},
): ScoredBook | null {
  if (profile.dismissed.has(book.id)) return null;
  if (readingState(book) === "finished") return null;

  const w = opts.weights ?? DEFAULT_WEIGHTS;
  const genreAff = book.genre ? squash(profile.genres[book.genre] ?? 0) : 0;
  const authorAff = squash(profile.authors[book.author] ?? 0);
  const eraAff = book.era ? squash(profile.eras[book.era] ?? 0) : 0;
  const popularity = opts.popularityRank ?? 0;
  // Novelty: brand-new, unread books get a small boost so the row isn't stale.
  const novelty = book.isNew && book.progress === 0 ? 1 : 0;

  const parts = {
    genre: genreAff * w.genre,
    author: authorAff * w.author,
    era: eraAff * w.era,
    popularity: popularity * w.popularity,
    novelty: novelty * w.novelty,
  };
  const score = parts.genre + parts.author + parts.era + parts.popularity + parts.novelty;

  return { book, score, reason: explain(book, parts), basis: dominantBasis(parts, book), parts };
}

function dominantBasis(parts: ScoredBook["parts"], book: DiscoveryBook): Recommendation["basis"] {
  const ranked: [Recommendation["basis"], number][] = [
    ["author", parts.author],
    ["genre", parts.genre],
    ["era", parts.era],
    ["popular", parts.popularity],
    ["new", parts.novelty],
  ];
  ranked.sort((a, b) => b[1] - a[1]);
  if (ranked[0][1] <= 0) return book.isNew ? "new" : "fresh";
  return ranked[0][0];
}

function explain(book: DiscoveryBook, parts: ScoredBook["parts"]): string {
  const max = Math.max(parts.author, parts.genre, parts.era, parts.popularity, parts.novelty);
  if (max <= 0) return "Picked for you";
  if (parts.author === max) return `More from ${book.author}`;
  if (parts.genre === max && book.genre) return `Because you read ${book.genre}`;
  if (parts.era === max && book.era) return `From the ${book.era}`;
  if (parts.novelty === max) return "New on Kinora";
  return "Popular right now";
}

/**
 * Rank a catalog into recommendations for a profile. Excludes dismissed/finished
 * (via scoreCandidate). `popularity` maps book id → 0..1 prior (optional).
 * Stable: ties break by the catalog's incoming order.
 */
export function recommend(
  books: DiscoveryBook[],
  profile: TasteProfile,
  opts: { weights?: ScoreWeights; popularity?: Record<string, number>; limit?: number } = {},
): Recommendation[] {
  const scored: (ScoredBook & { idx: number })[] = [];
  books.forEach((book, idx) => {
    const s = scoreCandidate(book, profile, {
      weights: opts.weights,
      popularityRank: opts.popularity?.[book.id],
    });
    if (s && s.score > 0) scored.push({ ...s, idx });
  });
  scored.sort((a, b) => b.score - a.score || a.idx - b.idx);
  const limited = opts.limit ? scored.slice(0, opts.limit) : scored;
  return limited.map(({ book, score, reason, basis }) => ({ book, score, reason, basis }));
}

/** Books "similar" to a seed: shares genre OR author OR era, ranked by overlap.
 *  Powers the "More like <title>" rail and the preview card's suggestions. */
export function similarTo(
  seed: DiscoveryBook,
  books: DiscoveryBook[],
  limit = 8,
): DiscoveryBook[] {
  const scored: { book: DiscoveryBook; score: number; idx: number }[] = [];
  books.forEach((book, idx) => {
    if (book.id === seed.id) return;
    let score = 0;
    if (seed.author && book.author === seed.author) score += 3;
    if (seed.genre && book.genre === seed.genre) score += 2;
    if (seed.era && book.era === seed.era) score += 1;
    if (score > 0) scored.push({ book, score, idx });
  });
  scored.sort((a, b) => b.score - a.score || a.idx - b.idx);
  return scored.slice(0, limit).map((s) => s.book);
}
