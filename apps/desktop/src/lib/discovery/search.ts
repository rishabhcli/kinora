// Faceted + ranked search over the discovery catalog. Pure: takes a book list +
// a query/facet selection, returns ranked hits. Built on the tokenizer's tiered
// field scorer so typos, prefixes and word-boundary matches all rank sensibly.
import type {
  DiscoveryBook,
  FacetSelection,
  ReadingState,
  SearchHit,
} from "./types";
import { uniqueTokens, tokenFieldScore, normalize } from "./tokenize";

/** Reading-state bucket from a progress percentage (0..100). */
export function readingState(book: DiscoveryBook): ReadingState {
  if (book.progress >= 100) return "finished";
  if (book.progress > 0) return "reading";
  return "unread";
}

/** Field weights: a title hit matters more than an era hit. */
const FIELD_WEIGHTS = {
  title: 1,
  author: 0.8,
  genre: 0.5,
  era: 0.35,
} as const;

type Field = keyof typeof FIELD_WEIGHTS;

function fieldValue(book: DiscoveryBook, field: Field): string {
  switch (field) {
    case "title":
      return book.title;
    case "author":
      return book.author;
    case "genre":
      return book.genre ?? "";
    case "era":
      return book.era ?? "";
  }
}

/** Relevance of one book to a free-text query. Every query token must hit SOME
 *  field (AND across tokens); within a token we take the best-scoring field.
 *  Returns `{ score, matchedFields }` or null when the book doesn't match all
 *  tokens. */
export function scoreBook(
  book: DiscoveryBook,
  query: string,
): { score: number; matchedFields: Field[] } | null {
  const tokens = uniqueTokens(query);
  if (tokens.length === 0) return { score: 0, matchedFields: [] };

  let total = 0;
  const matched = new Set<Field>();

  for (const token of tokens) {
    let bestForToken = 0;
    let bestField: Field | null = null;
    for (const field of Object.keys(FIELD_WEIGHTS) as Field[]) {
      const raw = tokenFieldScore(token, fieldValue(book, field));
      const weighted = raw * FIELD_WEIGHTS[field];
      if (weighted > bestForToken) {
        bestForToken = weighted;
        bestField = field;
      }
    }
    if (bestForToken === 0) return null; // a token matched nothing → drop the book
    total += bestForToken;
    if (bestField) matched.add(bestField);
  }

  // Average per token, then a small bonus for matching across multiple fields.
  const avg = total / tokens.length;
  const fieldBonus = 1 + 0.05 * (matched.size - 1);
  return { score: avg * fieldBonus, matchedFields: [...matched] };
}

/** Apply facet constraints (genre/era/author/state) — pure boolean filter. */
export function applyFacetConstraints(
  books: DiscoveryBook[],
  sel: FacetSelection,
): DiscoveryBook[] {
  const wantGenre = new Set((sel.genre ?? []).map(normalize));
  const wantEra = new Set((sel.era ?? []).map(normalize));
  const wantAuthor = new Set((sel.author ?? []).map(normalize));
  const wantState = new Set<ReadingState>(sel.state ?? []);

  return books.filter((b) => {
    if (wantGenre.size && !wantGenre.has(normalize(b.genre ?? ""))) return false;
    if (wantEra.size && !wantEra.has(normalize(b.era ?? ""))) return false;
    if (wantAuthor.size && !wantAuthor.has(normalize(b.author))) return false;
    if (wantState.size && !wantState.has(readingState(b))) return false;
    return true;
  });
}

/**
 * Full faceted search: apply facet constraints, then rank by free-text
 * relevance. With no text, returns the facet-filtered set in catalog order
 * (score 0). Stable: ties break by original index so results don't jitter.
 */
export function search(books: DiscoveryBook[], sel: FacetSelection): SearchHit[] {
  const constrained = applyFacetConstraints(books, sel);
  const text = (sel.text ?? "").trim();

  if (!text) {
    return constrained.map((book) => ({ book, score: 0, matchedFields: [] }));
  }

  const hits: (SearchHit & { idx: number })[] = [];
  constrained.forEach((book, idx) => {
    const scored = scoreBook(book, text);
    if (scored && scored.score > 0) {
      hits.push({ book, score: scored.score, matchedFields: scored.matchedFields, idx });
    }
  });

  hits.sort((a, b) => b.score - a.score || a.idx - b.idx);
  return hits.map(({ book, score, matchedFields }) => ({ book, score, matchedFields }));
}

/** Top-N quick suggestions for a search-as-you-type box (titles/authors only,
 *  deduped). Used by the search panel's instant dropdown. */
export function suggest(books: DiscoveryBook[], query: string, limit = 6): DiscoveryBook[] {
  const text = query.trim();
  if (!text) return [];
  return search(books, { text }).slice(0, limit).map((h) => h.book);
}

/** Did-you-mean: the single closest title when a query returns nothing. */
export function didYouMean(books: DiscoveryBook[], query: string): string | null {
  const text = normalize(query);
  if (!text || books.length === 0) return null;
  let best: { title: string; score: number } | null = null;
  for (const b of books) {
    // reuse the title fuzzy tier directly
    const s = tokenFieldScore(text.split(" ")[0] ?? text, b.title);
    if (s > 0 && (!best || s > best.score)) best = { title: b.title, score: s };
  }
  // 0.2 admits a single-typo fuzzy hit (the lowest tier in tokenFieldScore) but
  // not pure noise (which scores 0).
  return best && best.score >= 0.2 ? best.title : null;
}
