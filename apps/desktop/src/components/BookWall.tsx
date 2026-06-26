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

/** The login backdrop: stacked glass shelves of books receding into the dark.
 *  Each shelf glides horizontally (alternating direction), and a per-row depth
 *  (top shelves further back: smaller, dimmer, slower) gives the wall real
 *  parallax depth — pure transform/opacity, so it stays cheap and 60fps. The
 *  ambient lighting (projector beam, dust, vignette) is layered by AmbientBackdrop. */
export default function BookWall({
  rows = 4,
  parallax = 1,
}: {
  rows?: number;
  parallax?: number;
}) {
  return (
    <div aria-hidden className="bookwall pointer-events-none absolute inset-0 overflow-hidden">
      <div className="bookwall-shelves">
        {Array.from({ length: rows }).map((_, r) => {
          const slideRight = r % 2 === 1; // alternate direction every shelf
          // depth 0 = nearest (bottom), 1 = furthest (top). Far shelves are
          // smaller, dimmer and drift slower → the wall reads as deep.
          const depth = rows > 1 ? (rows - 1 - r) / (rows - 1) : 0;
          // Offset the pool per shelf so rows never line up, take enough to
          // overflow the viewport, then duplicate for a seamless loop.
          const start = (r * 5) % POOL.length;
          const rowBooks = POOL.slice(start).concat(POOL.slice(0, start)).slice(0, 14);
          const loop = rowBooks.concat(rowBooks);
          // Far shelves glide slower (longer duration) for a parallax feel; the
          // variant's `parallax` multiplier widens or tightens that spread.
          const duration = (118 + depth * 70 * parallax) | 0;
          // Whole shelf faces one way; shelves alternate, echoing the slide.
          const tilt = slideRight ? "-12deg" : "12deg";
          return (
            <div
              className="shelf"
              key={r}
              style={{ "--depth": depth.toFixed(3) } as CSSProperties}
            >
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

      {/* Edge fades so books glide off-screen smoothly and the rail never shows a
          hard seam. The key light + vignette live in AmbientBackdrop. */}
      <div className="bookwall-edge bookwall-edge--l" />
      <div className="bookwall-edge bookwall-edge--r" />
      <div className="bookwall-edge bookwall-edge--t" />
      <div className="bookwall-edge bookwall-edge--b" />
    </div>
  );
}
