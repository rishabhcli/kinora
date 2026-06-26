import { useState } from "react";
import { ShelfScroller } from "./ShelfScroller";
import { Tilt } from "./Tilt";
import { Reveal } from "./Reveal";
import { Pressable } from "./Pressable";
import { useMotion } from "./MotionProvider";
import {
  continueReading,
  recentlyAdded,
  popularOnKinora,
  recommended,
} from "../data/books";
import type { Book } from "../data/books";

/**
 * MotionShowcase — a gated demonstration of the motion primitives that
 * the product hasn't wired yet (ShelfScroller + Tilt are consumed by
 * Agent 5's book rows at integration). Reachable only via `?motiondemo`
 * so users never see it; it exists so the shelf-scroll + tilt signature
 * moments are demonstrable/capturable in isolation and to give the team a
 * living reference of the system.
 */

const SAMPLE: Book[] = [
  ...continueReading,
  ...recentlyAdded,
  ...popularOnKinora,
  ...recommended,
];

function DemoCover({ book }: { book: Book }) {
  return (
    <div className="flex-shrink-0" style={{ width: 150 }} data-shared-cover>
      <Tilt rotateDepth={12} translateDepth={16}>
        <div
          className="book-cover relative overflow-hidden"
          style={{
            width: 150,
            height: 225,
            borderRadius: 8,
            background: book.coverGradient,
            boxShadow: "0 18px 40px -16px rgba(0,0,0,0.7)",
          }}
        >
          {book.coverImage && (
            <img
              src={book.coverImage}
              alt={book.title}
              className="absolute inset-0 h-full w-full object-cover"
              onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
            />
          )}
          <div className="absolute inset-0" style={{ background: "linear-gradient(180deg, transparent 55%, rgba(0,0,0,0.5))" }} />
          <div className="absolute bottom-2 left-2 right-2">
            <p className="truncate text-[11px] font-semibold text-white/95">{book.title}</p>
            <p className="truncate text-[9px] text-white/65">{book.author}</p>
          </div>
        </div>
      </Tilt>
    </div>
  );
}

export function MotionShowcase() {
  const { speed, setSpeed, reduced } = useMotion();
  const [count, setCount] = useState(0);

  return (
    <div className="kinora-bg min-h-screen px-10 py-12 text-kinora-text" data-motion-showcase>
      <Reveal direction="up">
        <h1 className="font-serif text-3xl font-semibold">Kinora Motion System</h1>
        <p className="mt-1 text-[13px] text-kinora-muted">
          One instrument · gentle / snappy / cinematic · reduced-motion {reduced ? "ON" : "off"} · speed {speed.toFixed(2)}×
        </p>
      </Reveal>

      <section className="mt-10">
        <h2 className="mb-3 text-[13px] font-medium text-kinora-muted">
          ShelfScroller — drag, fling, wheel→horizontal, velocity snap, parallax, edge depth-of-field
        </h2>
        <ShelfScroller
          gap={16}
          railClassName="px-11 py-4"
          backdrop={<div className="h-full w-[200%] opacity-30" style={{ background: "radial-gradient(60% 80% at 20% 50%, rgba(212,164,78,0.18), transparent), radial-gradient(50% 70% at 70% 50%, rgba(120,140,200,0.15), transparent)" }} />}
        >
          {SAMPLE.map((b, i) => (
            <DemoCover key={`${b.id}-${i}`} book={b} />
          ))}
        </ShelfScroller>
      </section>

      <section className="mt-12">
        <h2 className="mb-3 text-[13px] font-medium text-kinora-muted">
          Reveal — staggered in-view entrance
        </h2>
        <Reveal stagger className="flex flex-wrap gap-4">
          {SAMPLE.slice(0, 8).map((b, i) => (
            <DemoCover key={`grid-${b.id}-${i}`} book={b} />
          ))}
        </Reveal>
      </section>

      <section className="mt-12 flex items-center gap-4">
        <h2 className="text-[13px] font-medium text-kinora-muted">Pressable + speed knob</h2>
        <Pressable
          className="glass-control rounded-lg px-4 py-2 text-[12px] font-medium"
          onClick={() => setCount((c) => c + 1)}
        >
          Pressed {count}×
        </Pressable>
        <input
          type="range"
          min={0.25}
          max={4}
          step={0.05}
          value={speed}
          onChange={(e) => setSpeed(parseFloat(e.target.value))}
          aria-label="Global motion speed"
          style={{ accentColor: "#d4a44e", width: 220 }}
        />
      </section>
    </div>
  );
}

export default MotionShowcase;
