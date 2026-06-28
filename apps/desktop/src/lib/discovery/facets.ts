// Facet derivation — compute the available filter dimensions + counts from a
// catalog, honoring the *other* active facets (so counts reflect what selecting
// a value would yield). Pure.
import type { DiscoveryBook, Facet, FacetSelection, ReadingState } from "./types";
import { applyFacetConstraints, readingState } from "./search";

const STATE_LABELS: Record<ReadingState, string> = {
  unread: "Not started",
  reading: "In progress",
  finished: "Finished",
};

/** Count occurrences of a string key across books, dropping empties. */
function countBy(books: DiscoveryBook[], pick: (b: DiscoveryBook) => string | undefined) {
  const counts = new Map<string, number>();
  for (const b of books) {
    const v = pick(b);
    if (!v) continue;
    counts.set(v, (counts.get(v) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([value, count]) => ({ value, count }));
}

/**
 * Derive the four facets (genre / era / author / state) from a catalog. Each
 * facet's counts are computed against the selection with that facet's own
 * dimension removed — the standard "faceted search" behavior where selecting a
 * genre doesn't zero out the other genres' counts.
 */
export function deriveFacets(books: DiscoveryBook[], sel: FacetSelection = {}): Facet[] {
  const withoutGenre = applyFacetConstraints(books, { ...sel, genre: [] });
  const withoutEra = applyFacetConstraints(books, { ...sel, era: [] });
  const withoutAuthor = applyFacetConstraints(books, { ...sel, author: [] });
  const withoutState = applyFacetConstraints(books, { ...sel, state: [] });

  const stateCounts = (["unread", "reading", "finished"] as ReadingState[])
    .map((value) => ({
      value,
      count: withoutState.filter((b) => readingState(b) === value).length,
    }))
    .filter((s) => s.count > 0);

  const facets: Facet[] = [
    { key: "genre", label: "Genre", values: countBy(withoutGenre, (b) => b.genre) },
    { key: "era", label: "Era", values: countBy(withoutEra, (b) => b.era) },
    { key: "author", label: "Author", values: countBy(withoutAuthor, (b) => b.author) },
    {
      key: "state",
      label: "Status",
      values: stateCounts.map((s) => ({ value: STATE_LABELS[s.value as ReadingState], count: s.count })),
    },
  ];
  return facets.filter((f) => f.values.length > 0);
}

/** Toggle a value in a facet selection array (immutably). */
export function toggleFacetValue(
  current: string[] | undefined,
  value: string,
): string[] {
  const set = new Set(current ?? []);
  if (set.has(value)) set.delete(value);
  else set.add(value);
  return [...set];
}

/** True if any facet (or text) is active. */
export function hasActiveFacets(sel: FacetSelection): boolean {
  return Boolean(
    (sel.text && sel.text.trim()) ||
      sel.genre?.length ||
      sel.era?.length ||
      sel.author?.length ||
      sel.state?.length,
  );
}

/** Total number of active filter chips, for a "Clear (N)" button. */
export function activeFacetCount(sel: FacetSelection): number {
  return (
    (sel.genre?.length ?? 0) +
    (sel.era?.length ?? 0) +
    (sel.author?.length ?? 0) +
    (sel.state?.length ?? 0)
  );
}

/** Map a human state label ("In progress") back to its ReadingState key. */
export function stateKeyFromLabel(label: string): ReadingState | null {
  const entry = (Object.entries(STATE_LABELS) as [ReadingState, string][]).find(
    ([, v]) => v === label,
  );
  return entry ? entry[0] : null;
}
