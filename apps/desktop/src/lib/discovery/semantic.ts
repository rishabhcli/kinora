// Lightweight client-side "semantic" similarity — no embeddings/network. We
// build a bag-of-tokens for each book (title + author + genre + era), expand a
// small hand-curated synonym map (so "space" ≈ "science fiction"), and score by
// weighted token overlap (Jaccard-ish with IDF-style rarity weighting). It's not
// a transformer, but it makes free-text browse queries like "space adventure" or
// "victorian romance" surface sensible books. Pure + deterministic.
import type { DiscoveryBook } from "./types";
import { uniqueTokens } from "./tokenize";

/** Query-expansion synonyms: a query token → extra tokens added to the query bag.
 *  Deliberately small + interpretable (no learned weights). */
const SYNONYMS: Record<string, string[]> = {
  space: ["science", "fiction", "sci"],
  scifi: ["science", "fiction"],
  sci: ["science", "fiction"],
  spaceship: ["science", "fiction", "space"],
  robot: ["science", "fiction"],
  future: ["science", "fiction"],
  ai: ["science", "fiction"],
  detective: ["mystery", "crime"],
  crime: ["mystery", "detective"],
  murder: ["mystery", "crime", "thriller"],
  thriller: ["mystery", "suspense"],
  love: ["romance"],
  romantic: ["romance"],
  victorian: ["19th", "century", "romance"],
  gothic: ["horror", "19th", "century"],
  magic: ["fantasy"],
  wizard: ["fantasy"],
  dragon: ["fantasy"],
  quest: ["adventure", "fantasy"],
  sea: ["adventure"],
  voyage: ["adventure"],
  war: ["historical", "adventure"],
  classic: ["literary", "literature"],
};

/** Expand a set of query tokens with synonyms (deduped). */
export function expandQuery(tokens: string[]): string[] {
  const out = new Set(tokens);
  for (const tok of tokens) {
    for (const syn of SYNONYMS[tok] ?? []) out.add(syn);
  }
  return [...out];
}

/** Build the per-book token bag (title/author/genre/era). */
export function bookBag(book: DiscoveryBook): Set<string> {
  return new Set(
    uniqueTokens([book.title, book.author, book.genre ?? "", book.era ?? ""].join(" ")),
  );
}

/** Document-frequency map: token → how many books contain it. Drives IDF so a
 *  rare token ("neuromancer") counts more than a common one ("the"). */
export function buildDocFreq(books: DiscoveryBook[]): Map<string, number> {
  const df = new Map<string, number>();
  for (const b of books) {
    for (const tok of bookBag(b)) df.set(tok, (df.get(tok) ?? 0) + 1);
  }
  return df;
}

function idf(token: string, df: Map<string, number>, total: number): number {
  const n = df.get(token) ?? 0;
  if (n === 0) return 0;
  return Math.log((total + 1) / (n + 0.5));
}

export interface SemanticHit {
  book: DiscoveryBook;
  score: number;
}

/**
 * Rank books by IDF-weighted overlap between the expanded query bag and each
 * book's token bag. Books with zero overlap are dropped. Stable on ties.
 */
export function semanticSearch(
  books: DiscoveryBook[],
  query: string,
  opts: { docFreq?: Map<string, number>; limit?: number } = {},
): SemanticHit[] {
  const qTokens = expandQuery(uniqueTokens(query));
  if (qTokens.length === 0) return [];
  const df = opts.docFreq ?? buildDocFreq(books);
  const total = books.length;

  const hits: (SemanticHit & { idx: number })[] = [];
  books.forEach((book, idx) => {
    const bag = bookBag(book);
    let score = 0;
    for (const tok of qTokens) {
      if (bag.has(tok)) score += idf(tok, df, total);
    }
    if (score > 0) hits.push({ book, score, idx });
  });

  hits.sort((a, b) => b.score - a.score || a.idx - b.idx);
  const limited = opts.limit ? hits.slice(0, opts.limit) : hits;
  return limited.map(({ book, score }) => ({ book, score }));
}
