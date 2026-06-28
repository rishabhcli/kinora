// RecommendationRail — a horizontal, scrollable shelf of preview cards with an
// optional "Because you read …" subtitle. Used for every discovery row. Scroll
// buttons appear at the edges; the row itself is a keyboard-focusable group with
// per-card preview cards (which carry their own focus + hover behavior).
import { useCallback, useEffect, useRef, useState } from "react";
import type { DiscoveryBook } from "../../lib/discovery/types";
import BookPreviewCard, { type PreviewActions } from "./BookPreviewCard";
import { ensureDiscoveryStyles } from "./styleInjection";

interface RecommendationRailProps extends PreviewActions {
  title: string;
  books: DiscoveryBook[];
  /** Optional subtitle/reason ("Because you read Science Fiction"). */
  reason?: string;
  /** Per-card reason lookup (overrides `reason` for individual cards). */
  reasonFor?: (book: DiscoveryBook) => string | undefined;
  /** Roving-tabindex: this rail's row index in the home grid + helpers. When
   *  omitted, every card is a normal tab stop (tabIndex 0). */
  rowIndex?: number;
  tabIndexFor?: (row: number, col: number) => 0 | -1;
  idFor?: (row: number, col: number) => string;
  onCellFocus?: (row: number, col: number) => void;
  "data-testid"?: string;
}

const Arrow = ({ dir }: { dir: "left" | "right" }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    {dir === "left" ? <path d="M19 12H5M11 6l-6 6 6 6" /> : <path d="M5 12h14M13 6l6 6-6 6" />}
  </svg>
);

export default function RecommendationRail({
  title,
  books,
  reason,
  reasonFor,
  rowIndex,
  tabIndexFor,
  idFor,
  onCellFocus,
  "data-testid": testId,
  ...actions
}: RecommendationRailProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [canLeft, setCanLeft] = useState(false);
  const [canRight, setCanRight] = useState(true);
  const raf = useRef(0);

  const update = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setCanLeft(el.scrollLeft > 4);
    setCanRight(el.scrollLeft < el.scrollWidth - el.clientWidth - 4);
  }, []);

  const schedule = useCallback(() => {
    if (raf.current) return;
    raf.current = requestAnimationFrame(() => {
      raf.current = 0;
      update();
    });
  }, [update]);

  useEffect(() => {
    ensureDiscoveryStyles();
    update();
  }, [books, update]);
  useEffect(() => () => { if (raf.current) cancelAnimationFrame(raf.current); }, []);

  const scrollBy = (dir: "left" | "right") => {
    scrollRef.current?.scrollBy({ left: dir === "left" ? -320 : 320, behavior: "smooth" });
  };

  if (books.length === 0) return null;

  return (
    <section className="mb-8 discovery-row-in" data-testid={testId} aria-label={title}>
      <div className="flex items-baseline gap-2 mb-3 px-1">
        <div
          className="w-1 h-4 rounded-full"
          style={{ background: "linear-gradient(180deg, rgba(212,164,78,0.8) 0%, rgba(212,164,78,0.3) 100%)" }}
          aria-hidden
        />
        <h2 className="font-serif text-base font-semibold text-kinora-text tracking-wide">{title}</h2>
        {reason && <span className="text-[11px] text-kinora-muted">· {reason}</span>}
      </div>

      <div className="relative group/rail">
        {canLeft && (
          <button
            aria-label={`Scroll ${title} left`}
            onClick={() => scrollBy("left")}
            className="absolute left-0 top-0 bottom-3 z-20 flex items-center justify-center w-8"
            style={{ background: "linear-gradient(90deg, rgba(15,14,12,0.8) 30%, transparent 100%)" }}
          >
            <Arrow dir="left" />
          </button>
        )}
        {canRight && (
          <button
            aria-label={`Scroll ${title} right`}
            onClick={() => scrollBy("right")}
            className="absolute right-0 top-0 bottom-3 z-20 flex items-center justify-center w-8"
            style={{ background: "linear-gradient(270deg, rgba(15,14,12,0.8) 30%, transparent 100%)" }}
          >
            <Arrow dir="right" />
          </button>
        )}
        <div
          ref={scrollRef}
          role="group"
          aria-label={`${title} books, scrollable`}
          tabIndex={0}
          className="flex gap-4 overflow-x-auto hide-scrollbar px-1 pb-3"
          onScroll={schedule}
        >
          {books.map((book, col) => {
            const roving = rowIndex !== undefined && tabIndexFor && idFor;
            return (
              <BookPreviewCard
                key={book.id}
                book={book}
                reason={reasonFor ? reasonFor(book) : reason}
                tabIndex={roving ? tabIndexFor(rowIndex, col) : 0}
                controlId={roving ? idFor(rowIndex, col) : undefined}
                onCellFocus={roving ? () => onCellFocus?.(rowIndex, col) : undefined}
                {...actions}
              />
            );
          })}
        </div>
      </div>
    </section>
  );
}
