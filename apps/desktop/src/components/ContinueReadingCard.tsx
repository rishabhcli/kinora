import type { Book } from "../data/books";
import { BookCoverImage } from "./SkeletonShimmer";

interface ContinueReadingCardProps {
  book: Book;
}

function ProgressRing({ progress, size = 28 }: { progress: number; size?: number }) {
  const stroke = 2;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (progress / 100) * circumference;

  return (
    <div className="relative flex-shrink-0" style={{ width: size, height: size }}>
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
      <span className="absolute inset-0 flex items-center justify-center text-[7px] font-bold text-white leading-none">
        {progress}%
      </span>
    </div>
  );
}

export default function ContinueReadingCard({ book }: ContinueReadingCardProps) {
  return (
    <div className="glass-card rounded-xl px-3 py-2 flex items-center gap-2.5 w-[260px] animate-fade-in mt-4">
      {/* Book cover mini */}
      <div
        className="w-10 h-14 rounded flex-shrink-0 relative overflow-hidden"
        style={{
          background: book.coverGradient,
          boxShadow: "0 2px 6px rgba(0,0,0,0.3)",
        }}
      >
        <BookCoverImage
          src={book.coverImage}
          alt={book.title}
          className="absolute inset-0 w-full h-full object-cover"
          fallbackBackground={book.coverGradient}
        />
        <div className="absolute inset-0 book-spine" />
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0">
        <p className="text-[8px] text-kinora-subtle uppercase tracking-wider mb-0.5">
          Continue Reading
        </p>
        <h3 className="text-[12px] font-semibold text-kinora-text truncate leading-tight">
          {book.title}
        </h3>
        <p className="text-[10px] text-kinora-muted truncate">{book.author}</p>
      </div>

      {/* Circle progress ring */}
      <div className="progress-ring-bg flex-shrink-0" style={{ width: 28, height: 28 }}>
        <ProgressRing progress={book.progress} size={28} />
      </div>
    </div>
  );
}
