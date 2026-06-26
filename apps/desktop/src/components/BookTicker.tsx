import { useState, useEffect, useRef, useMemo } from "react";
import {
  continueReading, recentlyAdded, popularOnKinora,
  recommended, awardWinners, type Book,
} from "../data/books";

const REAL_BOOKS: Book[] = [
  ...continueReading, ...recentlyAdded, ...popularOnKinora,
  ...recommended, ...awardWinners,
];

const FB_PALETTES: [string, string, string][] = [
  ["#1e3a5f", "#0d1f33", "#e8eef5"], ["#4a3728", "#2e2318", "#e8d5c0"],
  ["#2c3e50", "#1a252f", "#e5e5e5"], ["#8b1a3a", "#5a1025", "#f8e8d8"],
  ["#1a1a2e", "#0d0d1a", "#d4c5f0"], ["#2d5016", "#1a3009", "#e8f5e9"],
  ["#c0392b", "#7d2418", "#f8e8d8"], ["#1e8449", "#145a32", "#e8f5e9"],
  ["#e67e22", "#a85c15", "#f8e8d8"], ["#b8860b", "#8b6914", "#f8e8d8"],
];

interface SlideBook {
  id: string; title: string; author: string;
  coverImage: string; coverGradient: string; textColor: string; isReal: boolean;
}

function buildSlides(): SlideBook[] {
  const slides: SlideBook[] = [];
  REAL_BOOKS.forEach((b) => {
    slides.push({ id: b.id, title: b.title, author: b.author,
      coverImage: b.coverImage, coverGradient: b.coverGradient,
      textColor: b.textColor, isReal: true });
  });
  // Add a few fallback covers for variety
  let i = 0;
  while (slides.length < 12) {
    const p = FB_PALETTES[i % FB_PALETTES.length];
    slides.push({ id: `fb-${i}`, title: "", author: "",
      coverImage: "", coverGradient: `linear-gradient(135deg, ${p[0]}, ${p[1]})`,
      textColor: p[2], isReal: false });
    i++;
  }
  // Shuffle
  for (let i = slides.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [slides[i], slides[j]] = [slides[j], slides[i]];
  }
  return slides.slice(0, 10);
}

// Ken Burns transform presets — alternating zoom/pan directions
const KEN_BURNS = [
  { scale: 1.15, tx: "0%", ty: "0%", duration: 8 },
  { scale: 1.25, tx: "-5%", ty: "3%", duration: 8 },
  { scale: 1.15, tx: "5%", ty: "-3%", duration: 8 },
  { scale: 1.3, tx: "0%", ty: "5%", duration: 8 },
  { scale: 1.2, tx: "-3%", ty: "-2%", duration: 8 },
];

const SLIDE_DURATION = 6000; // 6s per slide
const FADE_DURATION = 1200;   // 1.2s crossfade

export default function BookTicker() {
  const slides = useMemo(() => buildSlides(), []);
  const [current, setCurrent] = useState(0);
  const [next, setNext] = useState(1);
  const [fading, setFading] = useState(false);
  const timerRef = useRef<number>(0);

  useEffect(() => {
    const cycle = () => {
      setFading(true);
      // Mid-fade: swap next
      timerRef.current = window.setTimeout(() => {
        setCurrent(prev => {
          const n = (prev + 1) % slides.length;
          setNext((n + 1) % slides.length);
          return n;
        });
        setFading(false);
      }, FADE_DURATION);
    };

    const interval = window.setInterval(cycle, SLIDE_DURATION);
    return () => {
      clearInterval(interval);
      clearTimeout(timerRef.current);
    };
  }, [slides.length]);

  const kb = KEN_BURNS[current % KEN_BURNS.length];
  const kbNext = KEN_BURNS[next % KEN_BURNS.length];

  return (
    <div aria-hidden className="absolute inset-0 overflow-hidden bg-black">
      {/* Current slide */}
      <SlideImage book={slides[current]} kb={kb} opacity={fading ? 0 : 1} />
      {/* Next slide (fades in during transition) */}
      <SlideImage book={slides[next]} kb={kbNext} opacity={fading ? 1 : 0} />

      {/* Cinematic gradient overlays */}
      <div className="absolute inset-0" style={{
        background: "radial-gradient(ellipse at center, rgba(0,0,0,0.65) 0%, rgba(0,0,0,0.3) 40%, rgba(0,0,0,0.5) 100%)",
      }} />
      <div className="absolute inset-0" style={{
        background: "linear-gradient(180deg, rgba(0,0,0,0.4) 0%, transparent 25%, transparent 75%, rgba(0,0,0,0.5) 100%)",
      }} />

      {/* Film grain texture */}
      <div className="absolute inset-0 opacity-[0.03]" style={{
        backgroundImage: "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='200' height='200' filter='url(%23n)'/%3E%3C/svg%3E\")",
      }} />
    </div>
  );
}

function SlideImage({ book, kb, opacity }: { book: SlideBook; kb: typeof KEN_BURNS[0]; opacity: number }) {
  const [imgError, setImgError] = useState(false);
  const showImg = book.isReal && book.coverImage && !imgError;

  return (
    <div
      className="absolute inset-0"
      style={{
        opacity,
        transition: `opacity ${FADE_DURATION}ms ease-in-out`,
      }}
    >
      {showImg ? (
        <img
          src={book.coverImage}
          alt=""
          className="h-full w-full object-cover"
          style={{
            transform: `scale(${kb.scale}) translate(${kb.tx}, ${kb.ty})`,
            transition: `transform ${kb.duration}s ease-out`,
          }}
          onError={() => setImgError(true)}
        />
      ) : (
        <div
          className="h-full w-full"
          style={{
            background: book.coverGradient,
            transform: `scale(${kb.scale}) translate(${kb.tx}, ${kb.ty})`,
            transition: `transform ${kb.duration}s ease-out`,
          }}
        />
      )}
    </div>
  );
}
