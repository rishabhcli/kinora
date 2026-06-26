import type { CSSProperties } from "react";
import {
  continueReading,
  recentlyAdded,
  popularOnKinora,
  recommended,
  awardWinners,
  type Book,
} from "../data/books";

// Every book we have, de-duplicated by id — the pool the shelves draw from.
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

/** A single book standing on a shelf: a real 3D object — coloured spine, cream
 *  page block, a slight turn for thickness — that reflects in the glass below.
 *  `flip` mirrors the binding to the opposite side (set per row). */
function ShelfBook({ book, flip = false }: { book: Book; flip?: boolean }) {
  return (
    <div className={`wallbook${flip ? " wallbook--flip" : ""}`}>
      <div className="wallbook-3d">
        {/* Solid body: back cover, spine, fore-edge (pages), head + tail. */}
        <div className="wallbook-back" />
        <div className="wallbook-spine" style={{ background: book.spineColor }} />
        <div className="wallbook-fore" />
        <div className="wallbook-head" />
        <div className="wallbook-tail" />

        {/* Front cover */}
        <div className="wallbook-face" style={{ background: book.coverGradient }}>
          <img
            src={book.coverImage}
            alt=""
            loading="lazy"
            draggable={false}
            className="wallbook-img"
            // A missing cover comes back from OpenLibrary as a blank/1px image —
            // hide it so the book's own gradient shows instead of bare white.
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.display = "none";
            }}
            onLoad={(e) => {
              const img = e.currentTarget as HTMLImageElement;
              if (img.naturalWidth <= 1) img.style.display = "none";
            }}
          />
          <div className="wallbook-gloss" />
        </div>
      </div>
    </div>
  );
}

/** The login backdrop: stacked glass shelves of books. Each shelf glides
 *  horizontally, alternating direction top-to-bottom, with the books reflected
 *  in the glass beneath them. */
export default function BookWall({ rows = 4 }: { rows?: number }) {
  return (
    <div aria-hidden className="bookwall pointer-events-none absolute inset-0 overflow-hidden">
      <div className="bookwall-shelves">
        {Array.from({ length: rows }).map((_, r) => {
          const slideRight = r % 2 === 1; // alternate direction every shelf
          // Offset the pool per shelf so rows never line up, take enough to
          // overflow the viewport, then duplicate for a seamless loop.
          const start = (r * 5) % POOL.length;
          const rowBooks = POOL.slice(start).concat(POOL.slice(0, start)).slice(0, 14);
          const loop = rowBooks.concat(rowBooks);
          // Slow, unhurried glide — staggered so shelves don't march in lock-step.
          const duration = 130 + (r % 3) * 26;
          // Whole shelf faces one way; shelves alternate, echoing the slide.
          const tilt = slideRight ? "-12deg" : "12deg";
          return (
            <div className="shelf" key={r}>
              <div
                className={`shelf-rail ${slideRight ? "shelf-rail--right" : "shelf-rail--left"}`}
                style={{ "--dur": `${duration}s`, "--tilt": tilt } as CSSProperties}
              >
                {loop.map((book, i) => (
                  <ShelfBook key={`${book.id}-${i}`} book={book} flip={slideRight} />
                ))}
              </div>
              <div className="shelf-glass" />
            </div>
          );
        })}
      </div>

      {/* Warm key light from above, a soft vignette, and edge fades so the
          login card stays legible and books glide off-screen smoothly. */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(85% 55% at 50% -8%, rgba(212,164,78,0.18), transparent 58%)",
        }}
      />
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(120% 95% at 50% 50%, transparent 40%, rgba(11,10,9,0.66))",
        }}
      />
      <div className="absolute inset-y-0 left-0 w-[12%] bg-gradient-to-r from-kinora-bg-deep to-transparent" />
      <div className="absolute inset-y-0 right-0 w-[12%] bg-gradient-to-l from-kinora-bg-deep to-transparent" />
      <div className="absolute inset-x-0 top-0 h-1/5 bg-gradient-to-b from-kinora-bg-deep/70 to-transparent" />
      <div className="absolute inset-x-0 bottom-0 h-1/4 bg-gradient-to-t from-kinora-bg-deep/85 to-transparent" />
    </div>
  );
}
