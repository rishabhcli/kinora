// ContinueReadingRow — the "pick up where you left off" shelf. Ranks in-progress
// books by resume-worthiness (continueReading.ts) and renders larger landscape
// tiles with a progress bar + a "last read" hint, distinct from the square cards
// in the recommendation rails.
import { useMemo } from "react";
import type { DiscoveryBook, Interaction } from "../../lib/discovery/types";
import { continueReadingRanked, lastTouchMap } from "../../lib/discovery/continueReading";
import { BookCoverImage } from "../SkeletonShimmer";
import type { PreviewActions } from "./BookPreviewCard";

interface ContinueReadingRowProps extends Pick<PreviewActions, "onOpen"> {
  books: DiscoveryBook[];
  history?: Interaction[];
  now?: number;
  title?: string;
}

/** Friendly relative time ("2d ago"), or null when unknown. */
function relTime(lastAt: number | null, now: number): string | null {
  if (lastAt === null) return null;
  const mins = Math.max(0, Math.round((now - lastAt) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return `${Math.round(days / 30)}mo ago`;
}

export default function ContinueReadingRow({
  books,
  history = [],
  now = Date.now(),
  title = "Continue Reading",
  onOpen,
}: ContinueReadingRowProps) {
  const entries = useMemo(
    () => continueReadingRanked(books, { lastTouch: lastTouchMap(history), now }),
    [books, history, now],
  );

  if (entries.length === 0) return null;

  return (
    <section className="mb-8" aria-label={title} data-testid="continue-reading-row">
      <div className="flex items-baseline gap-2 mb-3 px-1">
        <div
          className="w-1 h-4 rounded-full"
          style={{ background: "linear-gradient(180deg, rgba(212,164,78,0.8) 0%, rgba(212,164,78,0.3) 100%)" }}
          aria-hidden
        />
        <h2 className="font-serif text-base font-semibold text-kinora-text tracking-wide">{title}</h2>
      </div>

      <div className="flex gap-4 overflow-x-auto hide-scrollbar px-1 pb-3" role="group" aria-label={`${title}, scrollable`}>
        {entries.map(({ book, lastAt }) => {
          const rel = relTime(lastAt, now);
          return (
            <button
              key={book.id}
              onClick={() => onOpen?.(book)}
              aria-label={`Resume ${book.title} by ${book.author}, ${book.progress}% read${rel ? `, last read ${rel}` : ""}`}
              className="flex-shrink-0 w-[260px] text-left rounded-lg overflow-hidden group outline-none focus-visible:ring-2 focus-visible:ring-amber-400/70"
              style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)" }}
            >
              <div className="relative h-[120px]" style={{ background: book.coverGradient }}>
                <BookCoverImage
                  src={book.coverImage}
                  alt=""
                  className="absolute inset-0 w-full h-full object-cover"
                  fallbackBackground={book.coverGradient}
                />
                <div className="absolute inset-0" style={{ background: "linear-gradient(90deg, rgba(0,0,0,0.5) 0%, transparent 60%)" }} />
                <div className="absolute left-3 bottom-2 right-3">
                  <div className="flex items-center justify-center w-9 h-9 rounded-full mb-1" style={{ background: "rgba(212,164,78,0.95)" }}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="rgba(40,30,5,0.95)" aria-hidden>
                      <path d="M8 5v14l11-7z" />
                    </svg>
                  </div>
                </div>
              </div>
              <div className="p-2.5">
                <h3 className="text-[12px] font-medium text-kinora-text truncate leading-tight">{book.title}</h3>
                <p className="text-[10px] text-kinora-muted truncate mb-1.5">{book.author}</p>
                <div className="h-[3px] rounded-full bg-white/10 overflow-hidden">
                  <div className="h-full" style={{ width: `${book.progress}%`, background: "rgba(212,164,78,0.95)" }} />
                </div>
                <div className="flex items-center justify-between mt-1">
                  <span className="text-[9px] text-kinora-muted">{book.progress}%</span>
                  {rel && <span className="text-[9px] text-kinora-muted">{rel}</span>}
                </div>
              </div>
            </button>
          );
        })}
      </div>
    </section>
  );
}
