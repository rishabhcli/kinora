// buildCatalog — assemble the discovery catalog the home shell feeds to the
// engine. Merges the signed-in user's real backend library with the demo
// catalogue shelves (data/books) into a single deduped DiscoveryBook[], enriches
// genre/era from the bundled catalogue manifest, and derives a popularity prior.
//
// Pure (no React); the home shell calls it with the books it has on hand.
import type { Book } from "../../data/books";
import type { DiscoveryBook } from "../../lib/discovery/types";
import { CATALOG_META } from "../../data/catalog";

/** Attach genre/era from the catalogue manifest (by stable book id) if absent. */
export function enrich(book: Book & { genre?: string; era?: string }): DiscoveryBook {
  const meta = CATALOG_META[book.id];
  return {
    ...book,
    genre: book.genre ?? meta?.genre,
    era: book.era ?? meta?.era,
  };
}

/**
 * Merge multiple book sources into one deduped, enriched catalog. Earlier
 * sources win on id collision (so the user's real backend book is preferred over
 * a same-id demo entry). Order within the result is preserved (first-seen).
 */
export function mergeCatalog(...sources: (Book & { genre?: string; era?: string })[][]): DiscoveryBook[] {
  const seen = new Set<string>();
  const out: DiscoveryBook[] = [];
  for (const source of sources) {
    for (const b of source) {
      if (seen.has(b.id)) continue;
      seen.add(b.id);
      out.push(enrich(b));
    }
  }
  return out;
}

/**
 * Derive a popularity prior (0..1) from catalog position: books that appear in
 * the "popular" source rank highest. `popularIds` is an ordered id list (most
 * popular first); the rest get a small decayed prior by catalog order.
 */
export function popularityPrior(books: DiscoveryBook[], popularIds: string[]): Record<string, number> {
  const out: Record<string, number> = {};
  popularIds.forEach((id, i) => {
    out[id] = 1 - i / Math.max(popularIds.length, 1);
  });
  // give everything else a tiny baseline so ties have a stable, non-zero nudge
  books.forEach((b, i) => {
    if (out[b.id] === undefined) out[b.id] = Math.max(0, 0.2 - i * 0.005);
  });
  return out;
}
