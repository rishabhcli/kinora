import type { CSSProperties } from "react";
import {
  continueReading,
  recentlyAdded,
  popularOnKinora,
  recommended,
  awardWinners,
  type Book,
} from "../data/books";

// Every book we have, de-duplicated by id — the pool the wall scrolls through.
// Covers + titles come straight from the existing library data (unchanged).
const POOL: Book[] = (() => {
  const seen = new Set<string>();
  const pool: Book[] = [];
  for (const b of [
    ...continueReading,
    ...recentlyAdded,
    ...popularOnKinora,
    ...recommended,
    ...awardWinners,
  ]) {
    if (!seen.has(b.id)) {
      seen.add(b.id);
      pool.push(b);
    }
  }
  return pool;
})();

function WallCover({ book }: { book: Book }) {
  return (
    <div
      className="relative aspect-[2/3] w-full overflow-hidden rounded-[5px] shadow-[0_14px_30px_-10px_rgba(0,0,0,0.7)]"
      style={{ background: book.coverGradient }}
    >
      <img
        src={book.coverImage}
        alt=""
        loading="lazy"
        draggable={false}
        className="absolute inset-0 h-full w-full object-cover"
        onError={(e) => {
          (e.currentTarget as HTMLImageElement).style.opacity = "0";
        }}
      />
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "linear-gradient(105deg, rgba(255,255,255,0.16), transparent 38%, transparent 72%, rgba(0,0,0,0.3))",
        }}
      />
    </div>
  );
}

/** A living wall of book covers scrolling in alternating directions — the
 *  backdrop for the login screen. */
export default function BookWall({ columns = 5 }: { columns?: number }) {
  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-0 overflow-hidden bg-kinora-bg-deep"
    >
      <div
        className="absolute inset-0 grid gap-5 px-5"
        style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}
      >
        {Array.from({ length: columns }).map((_, col) => {
          // Rotate the pool per column so adjacent columns don't line up.
          const rotated = POOL.slice(col).concat(POOL.slice(0, col));
          const loop = rotated.concat(rotated); // duplicate for a seamless loop
          const duration = 78 + (col % 3) * 16;
          return (
            <div key={col} className="relative -my-24">
              <div
                className={col % 2 === 0 ? "bookwall-col-up" : "bookwall-col-down"}
                style={{ "--dur": `${duration}s` } as CSSProperties}
              >
                <div className="flex flex-col gap-5">
                  {loop.map((book, j) => (
                    <WallCover key={`${book.id}-${j}`} book={book} />
                  ))}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Warm gold light from above + a vignette + edge fades so the login
          card stays legible over the moving covers. */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(85% 55% at 50% -5%, rgba(212,164,78,0.20), transparent 55%)",
        }}
      />
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(120% 90% at 50% 50%, transparent 38%, rgba(14,13,12,0.62))",
        }}
      />
      <div className="absolute inset-x-0 top-0 h-1/4 bg-gradient-to-b from-kinora-bg-deep/70 to-transparent" />
      <div className="absolute inset-x-0 bottom-0 h-1/3 bg-gradient-to-t from-kinora-bg-deep/90 to-transparent" />
    </div>
  );
}
