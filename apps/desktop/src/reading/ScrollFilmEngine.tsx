import { useEffect, useMemo, useRef, type CSSProperties } from "react";
import type { Book } from "../data/books";
import { api, toBrowserUrl, type ShotResponse } from "../lib/api";
import {
  READING_FONTS,
  READING_THEMES,
  READING_SPACINGS,
  resolveEffectiveTheme,
  type ReadingPrefs,
  type ReadingTheme,
} from "../lib/readingPrefs";
import { buildTimeline, type SegmentInput, type Timeline } from "./timeline";
import { FilmPane, type FilmPaneHandle } from "./FilmPane";
import { useScrollFilm, type ScrollFrame } from "./useScrollFilm";
import { useReducedMotionPref } from "../a11y/useReducedMotionPref";

const FILM_W = 320;
const PARALLAX_PX = 12; // film drift over the full scroll (GPU translate; off when reduced)
const PARA_THROTTLE_MS = 100;

export interface ScrollFilmEngineProps {
  book: Book;
  pages: { n: number; text: string }[];
  shots: ShotResponse[];
  /** shot_id → browser-ready mp4 url (live SSE clip_ready map from the shell) */
  clips?: Record<string, string>;
  sessionId?: string | null;
  live?: boolean;
  /** bundled mp4 for the no-backend path; defaults to one chosen from book.id */
  fallbackFilm?: string;
  prefs: ReadingPrefs;
  effectiveTheme?: ReadingTheme;
  /** default: Kinora's app-wide reduced-motion preference. */
  reducedMotion?: boolean;
  bufferAhead?: number | null;
  bursting?: boolean;
  onProgress?: (fraction: number, focusWord: number) => void;
}

const FALLBACK_FILMS = [
  "/generated/film-01.mp4",
  "/generated/film-02.mp4",
  "/generated/film-03.mp4",
  "/generated/film-04.mp4",
];

const PLACEHOLDER = [
  "The first page felt heavy in her hands, as if the weight of every possible life pressed against her fingertips.",
  "Each book was a door, and each door led to a different version of the story — paths not taken, words not yet spoken.",
  "As the pages turned, the world rearranged itself a few seconds ahead, the way a film assembles just before you arrive.",
];

/** Build the scrubbing timeline from props. Live → one segment per shot (word
 *  range → its clip); no backend → a single segment spanning the whole bundled
 *  film. `buildTimeline` makes it contiguous so scrubbing never hits a dead zone. */
function timelineFromProps(
  shots: ShotResponse[],
  clips: Record<string, string>,
  live: boolean,
  fallbackFilm: string,
): Timeline {
  if (live) {
    const segs: SegmentInput[] = shots
      .filter((s) => s.source_span)
      .map((s) => ({
        id: s.shot_id,
        wordStart: s.source_span!.word_range[0],
        wordEnd: s.source_span!.word_range[1],
        src: clips[s.shot_id] ?? toBrowserUrl(s.clip_url) ?? "",
        duration: s.duration_s ?? undefined,
      }));
    if (segs.length > 0) return buildTimeline(segs);
  }
  // Fallback (or live with no shots yet): one continuous film. totalWords cancels
  // out of the time mapping (localFraction === scroll fraction), so 1000 is fine.
  return buildTimeline([{ id: "fallback", wordStart: 0, wordEnd: 1000, src: fallbackFilm }]);
}

/** The Scroll Film Engine: scrolling the book scrubs one continuous film. Owns the
 *  pinned vertical film pane, the scrolling text column, the progress/buffer rail,
 *  scroll↔film sync, parallax, and the scrub indicator. Mounted by the ReadingRoom
 *  shell (Agent 12), which keeps the chrome (top bar, cover-open, appearance). */
export function ScrollFilmEngine({
  book,
  pages,
  shots,
  clips = {},
  sessionId,
  live = false,
  fallbackFilm,
  prefs,
  effectiveTheme,
  reducedMotion: reducedMotionProp,
  bufferAhead = null,
  bursting = false,
  onProgress,
}: ScrollFilmEngineProps) {
  const autoReduce = useReducedMotionPref();
  const reduce = reducedMotionProp ?? autoReduce;

  const film =
    fallbackFilm ??
    FALLBACK_FILMS[[...book.id].reduce((a, c) => a + c.charCodeAt(0), 0) % FALLBACK_FILMS.length];

  const timeline = useMemo(
    () => timelineFromProps(shots, clips, live, film),
    [shots, clips, live, film],
  );

  const themeKey = effectiveTheme ?? resolveEffectiveTheme(prefs);
  const theme = READING_THEMES[themeKey];
  const sp = READING_SPACINGS[prefs.spacing];
  const font = READING_FONTS[prefs.fontFamily];
  // Show whatever text we were given; only fall back to placeholder copy when the
  // shell has none (mock books with no backend). `live` gates session behaviour,
  // not text rendering.
  const paragraphs = pages.length ? pages.map((p) => p.text) : PLACEHOLDER;
  const generating = live && Object.keys(clips).length === 0 && shots.length > 0;

  const scrollRef = useRef<HTMLDivElement>(null);
  const filmRef = useRef<FilmPaneHandle>(null);
  const parallaxRef = useRef<HTMLDivElement>(null);
  const railFillRef = useRef<HTMLDivElement>(null);
  const railLeadRef = useRef<HTMLDivElement>(null);
  const railDotRef = useRef<HTMLDivElement>(null);
  const scrubRef = useRef<HTMLDivElement>(null);
  const paraAt = useRef(0);
  const restored = useRef(false);
  // Cached paragraph metrics for the scroll-paint hot path [D1]: content-relative
  // tops measured once (and on resize / layout-pref change), so a fast flick never
  // reads layout (getBoundingClientRect) per node per frame.
  const paraNodes = useRef<HTMLElement[]>([]);
  const paraTops = useRef<number[]>([]);
  const lastActive = useRef(-1);
  const lastInk = useRef("");

  // Resume where the reader left off, once the column is tall enough to scroll.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || restored.current || el.scrollHeight <= el.clientHeight + 20) return;
    let saved = 0;
    try {
      saved = parseFloat(localStorage.getItem("kinora.read." + book.id) || "0");
    } catch {
      /* storage blocked */
    }
    if (saved > 0.02) el.scrollTo({ top: saved * (el.scrollHeight - el.clientHeight) });
    restored.current = true;
  }, [book.id, paragraphs.length, live]);

  // Paint the centred paragraph bright (others dimmed) — imperatively, so a fast
  // flick doesn't trigger a React re-render of the whole column.
  const setParaStyle = (p: HTMLElement, active: boolean) => {
    p.style.color = `rgba(${theme.ink}, ${active ? 1 : 0.62})`;
    p.style.borderLeftColor = active ? "rgba(212,164,78,0.7)" : "transparent";
  };

  const paintParagraph = () => {
    const sc = scrollRef.current;
    const nodes = paraNodes.current;
    const tops = paraTops.current;
    if (!sc || nodes.length === 0) return;
    // Active paragraph = last one whose top has crossed the 40% focus line, using
    // cached content offsets + the current scroll position (no layout reads here).
    const focusContentY = sc.scrollTop + sc.clientHeight * 0.4;
    let bestIndex = 0;
    for (let i = 0; i < tops.length; i++) {
      if (tops[i] <= focusContentY) bestIndex = i;
      else break; // paragraphs are in document order, so tops ascend
    }
    if (lastInk.current !== theme.ink) {
      // Theme changed → every paragraph needs the new ink.
      for (let i = 0; i < nodes.length; i++) setParaStyle(nodes[i], i === bestIndex);
      lastInk.current = theme.ink;
    } else if (bestIndex !== lastActive.current) {
      // Only the de-/re-activated paragraphs change.
      const prev = nodes[lastActive.current];
      if (prev) setParaStyle(prev, false);
      setParaStyle(nodes[bestIndex], true);
    }
    lastActive.current = bestIndex;
  };

  // Measure paragraph offsets after layout and whenever the text or a layout-
  // affecting reading pref changes; a ResizeObserver keeps them fresh on resize.
  useEffect(() => {
    const sc = scrollRef.current;
    if (!sc) return;
    const measure = () => {
      const nodes = Array.from(sc.querySelectorAll<HTMLElement>("[data-para]"));
      paraNodes.current = nodes;
      const contentTop = sc.getBoundingClientRect().top - sc.scrollTop;
      paraTops.current = nodes.map((n) => n.getBoundingClientRect().top - contentTop);
      lastActive.current = -1; // force a repaint against the fresh geometry
      lastInk.current = "";
      paintParagraph();
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(sc);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    paragraphs.length,
    prefs.fontScale,
    prefs.leading,
    prefs.spacing,
    prefs.measure,
    prefs.fontFamily,
    themeKey,
  ]);

  const onFrame = (frame: ScrollFrame) => {
    const pct = frame.fraction * 100;
    if (railFillRef.current) railFillRef.current.style.height = `${pct}%`;
    if (railDotRef.current) railDotRef.current.style.top = `calc(${pct}% - 4px)`;
    if (railLeadRef.current) railLeadRef.current.style.top = `${pct}%`;
    if (parallaxRef.current) {
      parallaxRef.current.style.transform = reduce
        ? "none"
        : `translate3d(0, ${(frame.fraction - 0.5) * PARALLAX_PX}px, 0)`;
    }
    if (scrubRef.current) {
      scrubRef.current.style.opacity = frame.mode === "scrub" && !reduce ? "1" : "0";
    }
    const now = performance.now();
    if (now - paraAt.current >= PARA_THROTTLE_MS) {
      paraAt.current = now;
      paintParagraph();
    }
  };

  useScrollFilm({
    scrollRef,
    filmRef,
    timeline,
    reducedMotion: reduce,
    onFrame,
    onProgress: (fraction, focusWord) => {
      try {
        if (restored.current) localStorage.setItem("kinora.read." + book.id, String(fraction));
      } catch {
        /* storage blocked */
      }
      onProgress?.(fraction, focusWord);
    },
    onSchedulerSignal: sessionId
      ? (sig) => {
          if (sig.kind === "seek") api.seek(sessionId, sig.word).catch(() => {});
          else api.postIntent(sessionId, sig.word, sig.velocity).catch(() => {});
        }
      : undefined,
  });

  return (
    <div className="mx-auto flex min-h-0 w-full max-w-[1180px] flex-1 items-stretch gap-10 overflow-hidden px-8 py-8">
      {/* Scrolling book text + reading-progress rail */}
      <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        <div
          ref={scrollRef}
          tabIndex={0}
          data-reading-scroll
          data-testid="reading-scroll"
          aria-label="Reading text — use arrow keys, space, or Page Up/Down to scroll"
          className="scrollbar-slim min-h-0 flex-1 overflow-y-auto pr-6 focus:outline-none"
        >
          <p className="mb-2 text-[10px] uppercase tracking-widest text-kinora-muted">Now Reading</p>
          <h1 className="mb-1 font-serif text-2xl font-semibold text-kinora-text">{book.title}</h1>
          <p className="mb-7 text-[13px] text-kinora-muted">by {book.author}</p>
          <div className="pb-[40vh]">
            <div
              className="mx-auto"
              style={{
                maxWidth: `${prefs.measure}ch`,
                background: theme.pageBg,
                color: `rgb(${theme.ink})`,
                borderRadius: theme.panel ? 16 : 0,
                padding: theme.panel ? "30px 34px" : 0,
                boxShadow: theme.panel && themeKey !== "night" ? "0 24px 70px -28px rgba(0,0,0,0.7)" : undefined,
                filter: prefs.brightness < 0.99 ? `brightness(${prefs.brightness})` : undefined,
                transition: reduce ? "none" : "background 0.3s ease, color 0.3s ease, filter 0.2s ease",
              }}
            >
              <div className="space-y-5" style={{ fontFamily: font.cssFamily }}>
                {paragraphs.map((para, i) => (
                  <p
                    key={i}
                    data-para={i}
                    className={font.className}
                    style={{
                      color: `rgba(${theme.ink}, ${i === 0 ? 1 : 0.62})`,
                      borderLeft: `2px solid ${i === 0 ? "rgba(212,164,78,0.7)" : "transparent"}`,
                      paddingLeft: 14,
                      fontSize: `${15 * prefs.fontScale}px`,
                      lineHeight: prefs.leading,
                      letterSpacing: sp.letter,
                      wordSpacing: sp.word,
                      transition: reduce ? "none" : "color 0.4s ease, border-color 0.4s ease",
                    }}
                  >
                    {para}
                  </p>
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* Read-so-far + committed-ahead rail; ticks = shots, dot = your place. */}
        <div className="pointer-events-none absolute right-1 top-1 bottom-1 w-1 rounded-full" aria-hidden style={{ background: "rgba(255,255,255,0.06)" }}>
          <div ref={railFillRef} className="absolute inset-x-0 top-0 rounded-full" style={{ height: "0%", background: "rgba(212,164,78,0.55)" }} />
          {live && (
            <div
              ref={railLeadRef}
              className="absolute inset-x-0 rounded-full"
              style={{
                top: "0%",
                height: `${Math.min(0.18, Math.max(0, (bufferAhead ?? 0) / 30)) * 100}%`,
                background: `linear-gradient(180deg, ${bursting ? "rgba(251,191,36,0.85)" : "rgba(52,211,153,0.75)"}, transparent)`,
                boxShadow: `0 0 8px ${bursting ? "rgba(251,191,36,0.55)" : "rgba(52,211,153,0.45)"}`,
              }}
            />
          )}
          {timeline.segments.map((s) => (
            <div
              key={s.id}
              className="absolute left-1/2 h-[2px] w-[7px] -translate-x-1/2 rounded-full"
              style={{ top: `${(s.wordStart / Math.max(1, timeline.totalWords)) * 100}%`, background: "rgba(255,255,255,0.22)" }}
            />
          ))}
          <div ref={railDotRef} className="absolute left-1/2 h-2 w-2 -translate-x-1/2 rounded-full" style={{ top: "0%", background: "#e8e2d8", boxShadow: "0 0 6px rgba(232,226,216,0.7)" }} />
        </div>
      </div>

      {/* Pinned vertical film (720×1280 / 9:16) */}
      <div className="flex-shrink-0 self-start">
        <div
          className="glass-card relative overflow-hidden rounded-[24px]"
          style={{ width: FILM_W, aspectRatio: "9 / 16", boxShadow: "0 28px 70px -18px rgba(0,0,0,0.7)" } as CSSProperties}
        >
          <div
            ref={parallaxRef}
            className="absolute"
            style={{ top: -16, bottom: -16, left: 0, right: 0, willChange: "transform" }}
          >
            <FilmPane
              ref={filmRef}
              poster={book.coverImage}
              reducedMotion={reduce}
              generating={generating}
              className="absolute inset-0"
            />
          </div>
          <div
            className="absolute left-3 top-3 flex items-center gap-1.5 rounded-full px-2.5 py-1"
            style={{ background: "rgba(0,0,0,0.42)", backdropFilter: "blur(10px)" }}
          >
            <span className="inline-flex h-1.5 w-1.5 rounded-full" style={{ background: "#34d399", boxShadow: "0 0 6px #34d399" }} />
            <span className="text-[9px] font-semibold tracking-wide text-white/90">AI FILM</span>
          </div>
          {/* Scrub indicator — fades in only while actively scrubbing */}
          <div
            ref={scrubRef}
            aria-hidden
            data-testid="scrub-indicator"
            className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full px-3 py-1"
            style={{
              opacity: 0,
              transition: reduce ? "none" : "opacity 0.18s ease",
              background: "rgba(0,0,0,0.5)",
              backdropFilter: "blur(8px)",
              willChange: "opacity",
            }}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#e8e2d8" strokeWidth={2} strokeLinecap="round">
              <path d="M8 5v14M16 5v14M3 12h18" />
            </svg>
            <span className="text-[9px] font-semibold tracking-wide text-white/90">SCRUBBING</span>
          </div>
        </div>
        <p className="mt-2.5 text-center text-[10px] text-kinora-muted">
          {live ? "Generated as you read · Wan" : "Generated with Wan · vertical short film"}
        </p>
      </div>
    </div>
  );
}

export default ScrollFilmEngine;
