import type React from "react";

/* A living, candle-lit backdrop that sits behind all page content (z-0).
   Two slow gold/amber auroras breathe in the corners, and a sparse field of
   warm "embers" drifts upward like dust in a sunbeam — pure CSS transform/opacity
   so it costs almost nothing, and it's switched off under prefers-reduced-motion
   (see .home-ambient / .ambient-mote in index.css). */

interface Mote {
  left: number; // vw
  size: number; // px
  duration: number; // s
  delay: number; // s (negative = already mid-flight on first paint)
  dx: number; // px horizontal drift
  opacity: number;
}

// Hand-tuned so the sparks feel scattered and unhurried rather than periodic.
const MOTES: Mote[] = [
  { left: 8, size: 3, duration: 26, delay: -4, dx: 40, opacity: 0.5 },
  { left: 19, size: 2, duration: 32, delay: -14, dx: -30, opacity: 0.4 },
  { left: 31, size: 4, duration: 23, delay: -9, dx: 24, opacity: 0.45 },
  { left: 44, size: 2, duration: 36, delay: -2, dx: 36, opacity: 0.35 },
  { left: 57, size: 3, duration: 28, delay: -18, dx: -28, opacity: 0.5 },
  { left: 68, size: 2, duration: 34, delay: -6, dx: 30, opacity: 0.38 },
  { left: 79, size: 4, duration: 25, delay: -12, dx: -22, opacity: 0.45 },
  { left: 90, size: 2, duration: 30, delay: -20, dx: 26, opacity: 0.4 },
];

// In the default `kinora-balanced` profile the motes are display:none and
// the auroras are static — skip the mote DOM entirely (saves 8 React
// elements + animation timeline allocations).
const isBalanced =
  typeof document !== "undefined" &&
  document.documentElement.classList.contains("kinora-balanced");

export default function AmbientBackground() {
  return (
    <div className="home-ambient" aria-hidden="true">
      <div className="home-aurora-blob home-aurora-blob--gold" />
      <div className="home-aurora-blob home-aurora-blob--amber" />
      {!isBalanced &&
        MOTES.map((m, i) => (
          <span
            key={i}
            className="ambient-mote"
            style={
              {
                left: `${m.left}vw`,
                width: m.size,
                height: m.size,
                animationDuration: `${m.duration}s`,
                animationDelay: `${m.delay}s`,
                "--mote-dx": `${m.dx}px`,
                "--mote-opacity": m.opacity,
              } as React.CSSProperties
            }
          />
        ))}
    </div>
  );
}
