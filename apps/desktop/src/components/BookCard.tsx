import { memo } from "react";
import type { Book } from "../data/books";
import { CometCard } from "./CometCard";
import { BookCoverImage } from "./SkeletonShimmer";

interface BookCardProps {
  book: Book;
  onOpen?: (book: Book) => void;
}

function ProgressRing({ progress }: { progress: number }) {
  const size = 22;
  const stroke = 1.5;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (progress / 100) * circumference;

  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="transform -rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          className="progress-ring-track"
          strokeWidth={stroke}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          className="progress-ring-fill"
          strokeWidth={stroke}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
        />
      </svg>
      <span className="absolute inset-0 flex items-center justify-center text-[6px] font-bold text-white leading-none" style={{ textShadow: '0 1px 2px rgba(0,0,0,0.8)' }}>
        {progress}%
      </span>
    </div>
  );
}

// Memoized: cards sit in long, staggered shelves that re-render on scroll /
// drag — skip re-rendering a card whose book + handler are unchanged.
const BookCard = memo(function BookCard({ book, onOpen }: BookCardProps) {
  return (
    <div
      className="flex-shrink-0 w-[150px] group cursor-pointer"
      style={{ perspective: 600 }}
      role="button"
      tabIndex={0}
      aria-label={`${book.title} by ${book.author}${book.genre ? `, ${book.genre}` : ""}${book.progress > 0 ? `, ${book.progress}% read` : ""}`}
      onClick={() => onOpen?.(book)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen?.(book);
        }
      }}
    >
      <CometCard rotateDepth={12} translateDepth={15}>
        <div className="book-3d-wrapper relative mb-1.5">
          <div
            className="book-cover w-[150px] relative"
            style={{ background: book.coverGradient }}
          >
            <div className="book-cover-inner">
              <BookCoverImage
                src={book.coverImage}
                alt={book.title}
                className="absolute inset-0 w-full h-full object-cover"
                fallbackBackground={book.coverGradient}
              />

              <div className="absolute inset-0 book-spine" />
              <div className="absolute inset-0 book-gloss" />

              {book.progress > 0 && (
                <div className="absolute top-1 right-1 progress-ring-bg" style={{ width: 22, height: 22 }}>
                  <ProgressRing progress={book.progress} />
                </div>
              )}

              {book.isNew && (
                <div className="absolute top-1 right-1 badge-new-gold px-1.5 py-0.5 text-[8px] font-bold text-amber-950">
                  New
                </div>
              )}

              {book.live && (
                <div
                  className="absolute bottom-1 left-1 flex items-center gap-1 rounded-full px-1.5 py-0.5"
                  style={{ background: "rgba(0,0,0,0.55)" }}
                >
                  <span className="inline-flex h-1.5 w-1.5 rounded-full" style={{ background: "#34d399", boxShadow: "0 0 5px #34d399" }} />
                  <span className="text-[7px] font-bold tracking-wider text-white/90">LIVE</span>
                </div>
              )}
            </div>
          </div>
        </div>
      </CometCard>

      {/* Title below cover */}
      <h3 className="text-[11px] font-medium text-kinora-text truncate leading-tight">
        {book.title}
      </h3>
      <p className="text-[10px] text-kinora-muted truncate">{book.author}</p>
      {book.genre && (
        <span
          className="inline-block mt-1 rounded px-1.5 py-px text-[8px] font-semibold tracking-wide uppercase"
          style={{ background: "rgba(212,164,78,0.15)", color: "rgba(212,164,78,0.92)" }}
        >
          {book.genre}
        </span>
      )}
    </div>
  );
});

export default BookCard;
