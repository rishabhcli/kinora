import { useState, useEffect, useRef, useCallback } from "react";
import { continueReading, type Book } from "../data/books";
import { BookCoverImage } from "./SkeletonShimmer";
import ContinueReadingCard from "./ContinueReadingCard";

const SLIDE_DURATION = 7000; // 7s per slide

const bookDescriptions: Record<string, string> = {
  "midnight-library":
    "Between life and death there is a library. When Nora Seed finds herself in the Midnight Library, she has a chance to make things right. A dazzling novel about all the other lives we could have lived.",
  "atomic-habits":
    "Tiny changes, remarkable results. James Clear reveals how small habits compound into life-altering transformations. A practical guide to building good habits and breaking bad ones.",
  "educated":
    "A searing memoir about a woman who leaves her survivalist family and goes on to earn a PhD from Cambridge. A story of the struggle for self-invention through education.",
  "project-hail-mary":
    "An astronaut wakes up alone on a spaceship with no memory. As fragments return, he realizes he's humanity's last hope — on an impossible mission to save Earth.",
  "sapiens":
    "From the dawn of cognition to the modern age, Yuval Noah Harari traces the journey of Homo sapiens. A sweeping narrative of how we came to dominate the planet.",
  "psychology-of-money":
    "Timeless lessons on wealth, greed, and happiness. Morgan Housel shows that doing well with money isn't about what you know — it's about how you behave.",
};

const bookBannerImages: Record<string, { src: string; contain?: boolean; position?: string }> = {};

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ];
}

function adjustColor([r, g, b]: [number, number, number], amt: number): string {
  return `rgb(${Math.max(0, Math.min(255, r + amt))}, ${Math.max(0, Math.min(255, g + amt))}, ${Math.max(0, Math.min(255, b + amt))})`;
}

function smartGradient(coverColor: string): string {
  const rgb = hexToRgb(coverColor);
  const lighter = adjustColor(rgb, 40);
  const darker = adjustColor(rgb, -60);
  const darkest = adjustColor(rgb, -90);
  return `radial-gradient(ellipse at 30% 40%, ${lighter} 0%, ${coverColor} 35%, ${darker} 70%, ${darkest} 100%)`;
}

function getSlide(book: Book) {
  return {
    ...book,
    description: bookDescriptions[book.id] ?? `${book.title} by ${book.author}.`,
    largeCover: book.coverImage.replace("-M.jpg", "-L.jpg"),
    banner: bookBannerImages[book.id],
    smartBg: smartGradient(book.coverColor),
  };
}

const slides = continueReading.map(getSlide);

interface DustMote {
  size: number;
  x: number;
  y: number;
  dx: number;
  dy: number;
  duration: number;
  delay: number;
}

const DUST_MOTES: DustMote[] = [
  { size: 3, x: 15, y: 70, dx: 30, dy: -40, duration: 12, delay: 0 },
  { size: 2, x: 70, y: 60, dx: 25, dy: -50, duration: 10, delay: 1 },
  { size: 3, x: 35, y: 65, dx: -35, dy: -35, duration: 11, delay: 2.5 },
  { size: 2, x: 65, y: 45, dx: 15, dy: -40, duration: 17, delay: 1.5 },
];

export default function HeroBanner() {
  const [current, setCurrent] = useState(0);
  const [paused, setPaused] = useState(false);
  const [visible, setVisible] = useState(true);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sectionRef = useRef<HTMLElement>(null);

  const goNext = useCallback(() => {
    setCurrent((prev) => (prev + 1) % slides.length);
  }, []);

  const goTo = useCallback((index: number) => {
    setCurrent(index);
  }, []);

  useEffect(() => {
    const el = sectionRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          setVisible(entry.isIntersecting);
        }
      },
      { rootMargin: "100px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (paused || !visible) return;
    timeoutRef.current = setTimeout(() => {
      goNext();
    }, SLIDE_DURATION);

    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, [current, paused, visible, goNext]);

  const slide = slides[current];

  return (
    <section
      ref={sectionRef}
      className="relative w-full h-[480px] overflow-hidden"
      style={{ contain: "paint" }}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
    >
      {/* Background — smart gradient + faint book cover */}
        <div
          key={`bg-${slide.id}`}
          className="absolute inset-0 hero-fade-in"
        >
          {/* Smart gradient base */}
          <div
            className="absolute inset-0"
            style={{ background: slide.smartBg }}
          />
        </div>

      {/* Floating dust motes — CSS only, no framer-motion */}
      {visible && (
      <div className="absolute inset-0 pointer-events-none overflow-hidden">
        {DUST_MOTES.map((mote, i) => (
          <div
            key={i}
            className="absolute rounded-full dust-mote"
            style={{
              width: mote.size,
              height: mote.size,
              background: "rgba(255, 255, 255, 0.15)",
              left: `${mote.x}%`,
              top: `${mote.y}%`,
              animationDuration: `${mote.duration}s`,
              animationDelay: `${mote.delay}s`,
              "--dx": `${mote.dx}px`,
              "--dy": `${mote.dy}px`,
            } as React.CSSProperties}
          />
        ))}
      </div>
      )}

      {/* Vignette + gradient overlays — merged into single layer */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background: [
            "radial-gradient(ellipse at center, transparent 50%, rgba(0,0,0,0.3) 100%)",
            "linear-gradient(180deg, rgba(15,14,12,0.3) 0%, transparent 30%, rgba(15,14,12,0.5) 60%, rgba(15,14,12,0.95) 100%)",
            "linear-gradient(90deg, rgba(15,14,12,0.7) 0%, rgba(15,14,12,0.3) 50%, transparent 100%)",
          ].join(", "),
        }}
      />

      {/* Content — two column: text on left, book cover on right */}
      <div className="absolute inset-0 flex items-end pt-16">
        <div className="max-w-[1280px] mx-auto w-full px-6 pb-8 relative flex items-end justify-between gap-8">
          {/* Left — text content */}
          <div className="flex-1 min-w-0">
            {/* Label */}
            <p
              key={`label-${slide.id}`}
              className="text-[10px] font-medium text-kinora-muted uppercase tracking-widest mb-2 hero-slide-up"
              style={{ animationDelay: "0.1s" }}
            >
              Featured Book
            </p>

            {/* Gold accent line */}
            <div
              key={`line-${slide.id}`}
              className="h-[2px] rounded-full mb-3 hero-line-grow"
              style={{ animationDelay: "0.2s", width: "48px", transformOrigin: "left center", background: "linear-gradient(90deg, rgba(212,164,78,0.6), rgba(212,164,78,0))" }}
            />

            {/* Title */}
              <h1
                key={`title-${slide.id}`}
                className="font-serif text-4xl font-semibold text-kinora-text mb-1 max-w-lg leading-tight hero-slide-up"
              >
                {slide.title}
              </h1>

            {/* Author */}
              <p
                key={`author-${slide.id}`}
                className="text-[14px] text-kinora-muted mb-3 hero-slide-up"
                style={{ animationDelay: "0.08s" }}
              >
                by {slide.author}
              </p>

            {/* Description */}
              <p
                key={`desc-${slide.id}`}
                className="text-[13px] text-kinora-muted max-w-md leading-relaxed mb-5 hero-slide-up"
                style={{ animationDelay: "0.05s" }}
              >
                {slide.description}
              </p>

            {/* Action buttons */}
            <div
              key={`actions-${slide.id}`}
              className="flex items-center gap-3 mb-4 hero-slide-up"
              style={{ animationDelay: "0.1s" }}
            >
              <button
                aria-label={`Read ${slide.title} now`}
                className="flex items-center gap-2 px-5 py-2.5 rounded-lg text-[13px] font-medium transition-transform"
                style={{
                  background: "rgba(232, 226, 216, 0.9)",
                  color: "#0f0e0c",
                }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style={{ transition: "transform 0.2s ease" }} className="hover:scale-110 origin-center">
                  <path d="M8 5v14l11-7z" />
                </svg>
                Read Now
              </button>
              <button
                aria-label={`Watch cinematic adaptation of ${slide.title}`}
                className="flex items-center gap-2 px-5 py-2.5 rounded-lg text-[13px] font-medium transition-colors"
                style={{
                  background: "rgba(255, 255, 255, 0.06)",
                  color: "rgba(232, 226, 216, 0.9)",
                  border: "1px solid rgba(255, 255, 255, 0.08)",
                }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="9" />
                  <path d="M10 8.5l5 3.5-5 3.5z" fill="currentColor" stroke="none" />
                </svg>
                Watch Cinematic
              </button>
            </div>

            {/* Continue Reading card */}
            <ContinueReadingCard book={slide} />

            {/* Progress bar indicators */}
            <div className="flex items-center gap-1.5 mt-4">
              {slides.map((s, i) => (
                <button
                  key={s.id}
                  onClick={() => goTo(i)}
                  className="flex-1 max-w-[40px] h-[3px] rounded-full overflow-hidden"
                  style={{
                    background: "rgba(255, 255, 255, 0.12)",
                    cursor: "pointer",
                  }}
                  aria-label={`Go to slide ${i + 1}`}
                >
                  <div
                    className={`h-full rounded-full origin-left ${i === current && !paused ? 'hero-progress-active' : ''}`}
                    style={{
                      width: "100%",
                      transform: i < current ? "scaleX(1)" : "scaleX(0)",
                      background:
                        i === current
                          ? "rgba(232, 226, 216, 0.9)"
                          : "rgba(232, 226, 216, 0.3)",
                    }}
                  />
                </button>
              ))}
            </div>
          </div>

          {/* Right — actual book cover, clearly visible */}
            <div
              key={`cover-${slide.id}`}
              className="hidden md:block flex-shrink-0 hero-cover-in"
            >
              <div
                className="rounded-lg overflow-hidden relative"
                style={{
                  width: 180,
                  height: 270,
                  background: slide.coverGradient,
                  boxShadow: "0 16px 48px rgba(0,0,0,0.6)",
                }}
              >
                <BookCoverImage
                  src={slide.largeCover}
                  alt={slide.title}
                  className="absolute inset-0 w-full h-full object-cover"
                  fallbackBackground={slide.coverGradient}
                />
                <div className="absolute inset-0 book-spine" />
                <div
                  className="absolute inset-0"
                  style={{
                    background:
                      "linear-gradient(135deg, rgba(255,255,255,0.08) 0%, transparent 30%, transparent 70%, rgba(255,255,255,0.02) 100%)",
                  }}
                />
              </div>
            </div>
        </div>
      </div>
    </section>
  );
}
