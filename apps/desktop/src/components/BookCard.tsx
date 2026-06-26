import { useState, type CSSProperties } from "react";
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

export default function BookCard({ book, onOpen }: BookCardProps) {
  const [opening, setOpening] = useState(false);

  const handleClick = () => {
    if (opening || !onOpen) return;
    setOpening(true);
    setTimeout(() => {
      onOpen(book);
      setOpening(false);
    }, 650);
  };

  return (
    <div
      className="flex-shrink-0 w-[150px] group cursor-pointer"
      style={{ perspective: opening ? 1400 : undefined, "--bt": "18px" } as CSSProperties}
      onClick={handleClick}
    >
      <CometCard rotateDepth={12} translateDepth={15}>
        <div
          className="book-3d-wrapper relative mb-1.5"
          style={{
            transformStyle: opening ? "preserve-3d" : "flat",
            transform: opening ? "scale(1.12)" : undefined,
            transition: opening ? "transform 0.5s cubic-bezier(0.34, 1.56, 0.64, 1)" : undefined,
          }}
        >
          {/* Static 3D body — page block, spine, back cover. Does NOT open with
              the cover, so the book stays a solid object. */}
          <div className="book-body" aria-hidden>
            <div className="book-back" />
            <div className="book-spine-face" style={{ background: book.spineColor }} />
            <div className="book-edge-top" />
            <div className="book-edge-bottom" />
            <div className="book-edge-right" />
          </div>

          {/* Page layers — visible underneath when the cover opens */}
          {opening && (
            <div
              className="absolute inset-0 rounded-[3px] overflow-hidden"
              style={{
                background: "linear-gradient(90deg, #e8e0d0 0%, #f5f0e8 8%, #faf6ee 100%)",
                boxShadow: "inset 2px 0 4px rgba(0,0,0,0.1), inset 0 0 20px rgba(180,160,130,0.15)",
                transformStyle: "preserve-3d",
              }}
            >
              {/* Stacked page lines for depth */}
              {[0, 1, 2, 3, 4].map((i) => (
                <div
                  key={i}
                  style={{
                    position: "absolute",
                    left: 0,
                    right: 0,
                    top: `${8 + i * 3}px`,
                    bottom: `${8 + i * 3}px`,
                    marginLeft: `${i * 1.5}px`,
                    background: i % 2 === 0 ? "rgba(200,190,170,0.08)" : "rgba(220,210,190,0.06)",
                    borderTop: "1px solid rgba(160,150,130,0.1)",
                  }}
                />
              ))}
              {/* Page text hint */}
              <div className="absolute inset-0 flex flex-col items-center justify-center opacity-30">
                <div className="w-[60%] h-[1px] bg-gray-400/30 mb-2" />
                <div className="w-[70%] h-[1px] bg-gray-400/25 mb-1.5" />
                <div className="w-[50%] h-[1px] bg-gray-400/20 mb-1.5" />
                <div className="w-[65%] h-[1px] bg-gray-400/25 mb-1.5" />
                <div className="w-[40%] h-[1px] bg-gray-400/15" />
              </div>
            </div>
          )}

          {/* Book cover — opens on left hinge with 3D depth */}
          <div
            className="book-cover w-[150px] relative"
            style={{
              background: book.coverGradient,
              transformOrigin: "left center",
              transformStyle: opening ? "preserve-3d" : "flat",
              boxShadow: opening
                ? "0 16px 40px rgba(0,0,0,0.5), -8px 0 20px rgba(0,0,0,0.3)"
                : undefined,
              transform: opening ? "rotateY(-125deg) translateZ(20px)" : undefined,
              transition: opening ? "transform 0.65s cubic-bezier(0.34, 1.56, 0.64, 1)" : undefined,
            }}
          >
            {/* Front face — cover image, spine, gloss. Hidden when rotated past 90° */}
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
            </div>

            {/* Back face — inside cover. Only rendered when opening */}
            {opening && (
            <div
              className="absolute inset-0 rounded-[3px] overflow-hidden"
              style={{
                background: "linear-gradient(135deg, rgba(40,35,30,0.98) 0%, rgba(60,50,40,0.95) 100%)",
                backfaceVisibility: "hidden",
                transform: "rotateY(180deg)",
              }}
            >
              <div className="absolute inset-0 flex flex-col items-center justify-center p-3">
                <div className="w-[70%] h-[1px] bg-white/10 mb-2" />
                <p className="font-serif text-[8px] text-white/40 text-center leading-tight">
                  {book.title}
                </p>
                <div className="w-[70%] h-[1px] bg-white/10 mt-2" />
              </div>
            </div>
            )}
          </div>
        </div>
      </CometCard>

      {/* Title below cover */}
      <h3 className="text-[11px] font-medium text-kinora-text truncate leading-tight">
        {book.title}
      </h3>
      <p className="text-[10px] text-kinora-muted truncate">{book.author}</p>
    </div>
  );
}
