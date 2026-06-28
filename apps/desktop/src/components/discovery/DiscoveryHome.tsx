// DiscoveryHome — the personalized home body. Composes useDiscovery (taste +
// rows) with the rail/continue-reading components. Renders the Continue Reading
// row from its own ranker, then the personalized rows, threading every card's
// open/preview/dismiss back into the discovery store so recommendations learn.
//
// Loading: when `loading` is set and there are no books yet, shows skeletons.
import { useMemo } from "react";
import type { DiscoveryBook } from "../../lib/discovery/types";
import { useDiscovery } from "./useDiscovery";
import type { KeyValueStore } from "../../lib/discovery/history";
import RecommendationRail from "./RecommendationRail";
import ContinueReadingRow from "./ContinueReadingRow";
import { DiscoveryHomeSkeleton } from "./RowSkeleton";
import { useRovingGrid } from "./useRovingGrid";

interface DiscoveryHomeProps {
  books: DiscoveryBook[];
  onOpenBook?: (book: DiscoveryBook) => void;
  /** Surface a "more like this" search for a seed book. */
  onMoreLikeThis?: (book: DiscoveryBook) => void;
  loading?: boolean;
  popularity?: Record<string, number>;
  /** Injectable seam + clock for tests. */
  store?: KeyValueStore;
  now?: () => number;
}

export default function DiscoveryHome({
  books,
  onOpenBook,
  onMoreLikeThis,
  loading = false,
  popularity,
  store,
  now,
}: DiscoveryHomeProps) {
  const discovery = useDiscovery(books, { store, popularity, now });

  const open = (book: DiscoveryBook) => {
    discovery.record(book, "open");
    onOpenBook?.(book);
  };
  const preview = (book: DiscoveryBook) => discovery.record(book, "preview");
  const dismiss = (book: DiscoveryBook) => discovery.dismiss(book);

  const inProgress = useMemo(
    () => books.filter((b) => b.progress > 0 && b.progress < 100),
    [books],
  );

  // The Continue Reading row is rendered separately (its own tile style). Filter
  // out the engine's `continue` row AND remove in-progress books from the rails
  // so a book you're mid-way through doesn't also appear in Popular/Top Picks.
  const railRows = useMemo(() => {
    const inProgressIds = new Set(inProgress.map((b) => b.id));
    return discovery.rows
      .filter((r) => r.kind !== "continue")
      .map((r) => ({ ...r, books: r.books.filter((b) => !inProgressIds.has(b.id)) }))
      .filter((r) => r.books.length > 0);
  }, [discovery.rows, inProgress]);

  // Roving-tabindex grid across the rails: one row per rail, one cell per card.
  // Arrow keys move within/across rails as a single tab stop (WAI-ARIA pattern).
  const rowSizes = useMemo(() => railRows.map((r) => r.books.length), [railRows]);
  const grid = useRovingGrid(rowSizes, "discovery");

  if (loading && books.length === 0) {
    return (
      <div className="max-w-[1280px] mx-auto px-6 pt-6">
        <DiscoveryHomeSkeleton rows={4} />
      </div>
    );
  }

  return (
    <div className="max-w-[1280px] mx-auto px-6 pt-6">
      <ContinueReadingRow
        books={inProgress}
        history={discovery.history}
        now={now ? now() : undefined}
        onOpen={open}
      />
      {/* The rails form one roving-tabindex grid: a single Tab stop, arrow keys
          move between cards and across rows. */}
      <div onKeyDown={grid.onKeyDown} role="grid" aria-label="Recommended shelves">
        {railRows.map((row, rowIndex) => (
          <RecommendationRail
            key={row.id}
            title={row.title}
            books={row.books}
            reason={row.reason}
            data-testid={`rail-${row.id}`}
            rowIndex={rowIndex}
            tabIndexFor={grid.tabIndexFor}
            idFor={grid.idFor}
            onCellFocus={(r, c) => grid.setActive({ row: r, col: c })}
            onOpen={open}
            onPreview={preview}
            onMoreLikeThis={onMoreLikeThis}
            onNotInterested={dismiss}
          />
        ))}
      </div>
    </div>
  );
}
