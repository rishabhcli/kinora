// Personalized home rows (Netflix-style) — composes the engine cores into an
// ordered list of shelves. Pure: catalog + profile + history → rows. Each row is
// deduped against earlier rows so a book doesn't appear twice above the fold.
import type { DiscoveryBook, DiscoveryRow, Interaction, TasteProfile } from "./types";
import { isColdStart, topAffinities } from "./affinity";
import { recommend } from "./scoring";
import { continueReadingBooks, lastTouchMap } from "./continueReading";
import { readingState } from "./search";

export interface BuildRowsOptions {
  profile: TasteProfile;
  history?: Interaction[];
  popularity?: Record<string, number>;
  now?: number;
  /** Min books for a row to be shown (avoid a lonely 1-card shelf). */
  minRowSize?: number;
  /** Max books per row. */
  maxRowSize?: number;
}

function take(books: DiscoveryBook[], seen: Set<string>, max: number): DiscoveryBook[] {
  const out: DiscoveryBook[] = [];
  for (const b of books) {
    if (seen.has(b.id)) continue;
    out.push(b);
    if (out.length >= max) break;
  }
  return out;
}

/**
 * Build the ordered home rows. Order:
 *  1. Continue Reading (if any in-progress)
 *  2. Top Picks (personalized) — or "Popular on Kinora" on a cold start
 *  3. "More <genre>" rows for the reader's top genres
 *  4. New on Kinora (recently added, unread)
 *  5. (cold start) Popular on Kinora
 * Books are deduped across rows so the page reads as distinct shelves.
 */
export function buildRows(books: DiscoveryBook[], opts: BuildRowsOptions): DiscoveryRow[] {
  const minRow = opts.minRowSize ?? 3;
  const maxRow = opts.maxRowSize ?? 20;
  const now = opts.now ?? Date.now();
  const rows: DiscoveryRow[] = [];
  const seen = new Set<string>();

  const pushRow = (row: Omit<DiscoveryRow, "books"> & { books: DiscoveryBook[] }) => {
    const trimmed = row.books.slice(0, maxRow);
    if (trimmed.length < minRow) return;
    rows.push({ ...row, books: trimmed });
    for (const b of trimmed) seen.add(b.id);
  };

  // 1. Continue Reading — always first when present (don't mark seen so a book
  //    you're reading can still appear in a themed row below if relevant... but
  //    that double-shows it; we DO mark it seen for a clean page).
  const lastTouch = lastTouchMap(opts.history ?? []);
  const resume = continueReadingBooks(books, { lastTouch, now });
  pushRow({ id: "continue", kind: "continue", title: "Continue Reading", books: resume });

  const cold = isColdStart(opts.profile);

  // 2. Top Picks (personalized recs) — a curated, capped mix. On a cold start
  //    this is empty → fall back to popular below. Capped (default 12) so it
  //    doesn't swallow whole genres that the themed rows below want to show.
  const topPicksCap = Math.min(maxRow, 12);
  if (!cold) {
    const recs = recommend(books, opts.profile, { popularity: opts.popularity });
    const top = take(recs.map((r) => r.book), seen, topPicksCap);
    pushRow({ id: "top-picks", kind: "recommended", title: "Top Picks for You", books: top });
  }

  // 3. "More <genre>" for the reader's top genres. Thematic rows draw from the
  //    whole genre — mild overlap with Top Picks is expected and Netflix-like —
  //    but they still don't repeat a book WITHIN the row, and they mark their
  //    books seen so the generic New/Popular tail below stays distinct.
  for (const genre of topAffinities(opts.profile.genres, 3)) {
    const inGenre = books.filter((b) => b.genre === genre && readingState(b) !== "finished");
    const picks = take(inGenre, new Set(), maxRow); // ignore `seen` for thematic rows
    if (picks.length >= minRow) {
      rows.push({
        id: `genre-${genre}`,
        kind: "genre",
        title: `More ${genre}`,
        reason: `Because you read ${genre}`,
        books: picks,
      });
      for (const b of picks) seen.add(b.id);
    }
  }

  // 4. New on Kinora — newest, unread, not already shown.
  const fresh = books.filter((b) => b.isNew && readingState(b) === "unread");
  pushRow({ id: "new", kind: "new", title: "New on Kinora", books: take(fresh, seen, maxRow) });

  // 5. Popular on Kinora — by popularity prior, then catalog order.
  const byPop = [...books]
    .filter((b) => readingState(b) !== "finished")
    .sort((a, b) => (opts.popularity?.[b.id] ?? 0) - (opts.popularity?.[a.id] ?? 0));
  pushRow({
    id: "popular",
    kind: "popular",
    title: "Popular on Kinora",
    books: take(byPop, seen, maxRow),
  });

  // 6. Rediscover — finished books, for a re-watch.
  const finished = books.filter((b) => readingState(b) === "finished");
  pushRow({
    id: "rediscover",
    kind: "rediscover",
    title: "Watch Again",
    books: take(finished, new Set(), maxRow), // finished aren't in `seen`
  });

  return rows;
}
