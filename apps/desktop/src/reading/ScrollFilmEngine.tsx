import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties } from "react";
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
import {
  buildTimeline,
  nextSegmentToPreload,
  resolvePlayhead,
  type SegmentInput,
  type Timeline,
} from "./timeline";
import { FilmPane, type FilmPaneHandle } from "./FilmPane";
import { useScrollFilm, type ScrollFrame } from "./useScrollFilm";
import { activeParagraphIndex, focusContentY, focusOpacity } from "./focusModel";
import { ClipCache } from "./clipCache";
import { useReducedMotionPref } from "../a11y/useReducedMotionPref";

// How far ahead/behind (in words) to keep neighbour clips warm so a cut in either
// direction is instant. Scroll-BACK is the priority: the previous shot's element
// stays decoded in the FilmPane pool, the previous clip's bytes stay in the cache.
const PRELOAD_LOOKAHEAD_WORDS = 240;

const FILM_W = 320;
const FILM_MIN = 200;
const FILM_MAX = 560;
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
  cache: ClipCache,
): Timeline {
  if (live) {
    const segs: SegmentInput[] = shots
      .filter((s) => s.source_span)
      .map((s) => {
        // The clip URL for this shot: the live SSE map first, else the persisted
        // clip_url from getShots (already browser-ready in the seed; defensively
        // rewritten here too). Resolve it THROUGH the cache so a clip whose bytes
        // are already in memory plays back from its stable blob URL — instant
        // replay on scroll-back, no network round-trip — while an uncached clip
        // keeps its network URL (and the cache warms it in the background).
        const url = clips[s.shot_id] ?? toBrowserUrl(s.clip_url) ?? "";
        return {
          id: s.shot_id,
          wordStart: s.source_span!.word_range[0],
          wordEnd: s.source_span!.word_range[1],
          src: cache.resolve(url),
          duration: s.duration_s ?? undefined,
        };
      });
    if (segs.length > 0) {
      // Seeded public-domain books ship one Ken-Burns clip on shot 0; forward-fill
      // so scroll-scrubbing keeps the film visible instead of blank segments.
      let carry = "";
      for (const seg of segs) {
        if (seg.src) carry = seg.src;
        else if (carry) seg.src = carry;
      }
      return buildTimeline(segs);
    }
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

  // Draggable splitter: film pane width is state-driven so the reader can resize.
  const [filmWidth, setFilmWidth] = useState(FILM_W);
  const containerRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);

  const onSplitterDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    draggingRef.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!draggingRef.current) return;
      const container = containerRef.current;
      if (!container) return;
      const rect = container.getBoundingClientRect();
      // Film is on the LEFT; new width = distance from the container's left edge to
      // the cursor.
      const newWidth = e.clientX - rect.left;
      setFilmWidth(Math.max(FILM_MIN, Math.min(FILM_MAX, newWidth)));
    };
    const onUp = () => {
      if (draggingRef.current) {
        draggingRef.current = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      }
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
  }, []);

  const film =
    fallbackFilm ??
    FALLBACK_FILMS[[...book.id].reduce((a, c) => a + c.charCodeAt(0), 0) % FALLBACK_FILMS.length];

  // Per-mount clip cache: holds each shot's mp4 bytes as a stable blob URL so a
  // scroll-BACK to an earlier shot replays the same clip instantly. Lives for the
  // engine's lifetime; cleared (blob URLs revoked) on unmount/book-change.
  const cacheRef = useRef<ClipCache | null>(null);
  if (!cacheRef.current) cacheRef.current = new ClipCache();
  const cache = cacheRef.current;
  // Re-resolve the timeline when the cache's blob-URL set changes (a clip finished
  // caching), so segments switch from their network URL to the cached blob URL.
  const cacheVersion = useSyncExternalStore(
    (cb) => cache.subscribe(cb),
    () => cache.version(),
    () => 0,
  );
  useEffect(() => () => cache.clear(), [cache]); // revoke all blob URLs on unmount
  useEffect(() => {
    cache.clear();
  }, [book.id, cache]);

  const timeline = useMemo(
    () => timelineFromProps(shots, clips, live, film, cache),
    [shots, clips, live, film, cache, cacheVersion],
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
  const preloadAt = useRef(0);
  const restored = useRef(false);
  // Latest timeline in a ref so the per-frame preload reads the current segments
  // (the onFrame callback is recreated each render, but keep this cheap + explicit).
  const timelineRef = useRef(timeline);
  timelineRef.current = timeline;
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

  // Paint a paragraph at its gentle focus opacity (active = 1, soft falloff toward
  // a comfortable floor). No hard 62% dim and no gold rule — Apple-Books calm. The
  // active paragraph keeps a barely-there weight cue via a hair-thin border.
  const setParaStyle = (p: HTMLElement, distance: number) => {
    p.style.color = `rgba(${theme.ink}, ${focusOpacity(distance)})`;
    p.style.borderLeftColor = distance === 0 ? "rgba(212,164,78,0.35)" : "transparent";
  };

  // The band of paragraphs whose opacity meaningfully differs from the floor; only
  // these need repainting when the active paragraph moves by one (everything beyond
  // is already at the floor). Keep it in sync with focusOpacity's falloff (2).
  const FOCUS_BAND = 3;

  const paintParagraph = () => {
    const sc = scrollRef.current;
    const nodes = paraNodes.current;
    const tops = paraTops.current;
    if (!sc || nodes.length === 0) return;
    const bestIndex = activeParagraphIndex(tops, focusContentY(sc.scrollTop, sc.clientHeight));
    if (lastInk.current !== theme.ink) {
      // Theme (ink) changed → repaint every paragraph against the new ink.
      for (let i = 0; i < nodes.length; i++) setParaStyle(nodes[i], i - bestIndex);
      lastInk.current = theme.ink;
    } else if (bestIndex !== lastActive.current) {
      // Repaint the focus band around BOTH the old and new active rows so the soft
      // ramp updates without touching the whole column.
      const lo = Math.min(bestIndex, lastActive.current) - FOCUS_BAND;
      const hi = Math.max(bestIndex, lastActive.current) + FOCUS_BAND;
      for (let i = Math.max(0, lo); i <= Math.min(nodes.length - 1, hi); i++) {
        setParaStyle(nodes[i], i - bestIndex);
      }
    }
    lastActive.current = bestIndex;
  };

  // Measure paragraph offsets after layout and whenever the text or a layout-
  // affecting reading pref changes. Resize robustness [C]: observe the scroll
  // element AND its content wrapper (a mid-scroll reflow that doesn't resize the
  // scroller — e.g. a font finishing loading, or the film pane resizing the row —
  // still re-measures), plus window resize. rAF-coalesced so a burst of resize
  // callbacks measures once per frame, never mid-paint.
  useEffect(() => {
    const sc = scrollRef.current;
    if (!sc) return;
    let pending = 0;
    const measureNow = () => {
      pending = 0;
      const nodes = Array.from(sc.querySelectorAll<HTMLElement>("[data-para]"));
      paraNodes.current = nodes;
      const contentTop = sc.getBoundingClientRect().top - sc.scrollTop;
      paraTops.current = nodes.map((n) => n.getBoundingClientRect().top - contentTop);
      lastActive.current = -1; // force a repaint against the fresh geometry
      lastInk.current = "";
      paintParagraph();
    };
    const measure = () => {
      if (pending) return;
      pending = requestAnimationFrame(measureNow);
    };
    measureNow();
    const ro = new ResizeObserver(measure);
    ro.observe(sc);
    const content = sc.querySelector<HTMLElement>("[data-reading-content]");
    if (content) ro.observe(content);
    window.addEventListener("resize", measure);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
      if (pending) cancelAnimationFrame(pending);
    };
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
    // Keep neighbour clips warm so a cut in EITHER direction is instant: prefetch
    // their bytes into the cache and pre-mount their decoded <video> in the
    // FilmPane pool. Throttled (cheap, idempotent) and direction-agnostic — the
    // previous shot is preloaded too, which is what makes scroll-BACK seamless.
    if (now - preloadAt.current >= PARA_THROTTLE_MS) {
      preloadAt.current = now;
      preloadNeighbours(frame.focusWord);
    }
  };

  // Warm the segments adjacent to the reader (prev + next) in the cache and the
  // FilmPane element pool, so transitions don't stall and scroll-back replays the
  // exact same decoded clip.
  const preloadNeighbours = (focusWord: number) => {
    const tl = timelineRef.current;
    if (!live || tl.segments.length === 0) return;
    const head = resolvePlayhead(tl, focusWord);
    if (!head) return;
    const prev = tl.segments[head.index - 1];
    const next = nextSegmentToPreload(tl, focusWord, PRELOAD_LOOKAHEAD_WORDS);
    for (const seg of [prev, next]) {
      if (!seg?.src) continue;
      cache.prefetch(seg.src); // warm the bytes (no-op if already cached)
      filmRef.current?.warm(seg.src); // pre-mount the decoded element off-screen
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
    <div ref={containerRef} className="mx-auto flex min-h-0 w-full max-w-[1180px] flex-1 items-stretch overflow-hidden px-8 py-8">
      {/* Pinned vertical film — now on the LEFT */}
      <div className="flex-shrink-0 self-start" style={{ width: filmWidth }}>
        <div
          className="glass-card relative overflow-hidden rounded-xl"
          style={{ width: filmWidth, aspectRatio: "9 / 16", boxShadow: "inset 0 1px 0 rgba(246,240,231,0.1), 0 34px 90px -22px rgba(6,5,4,0.85)" } as CSSProperties}
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
            className="absolute left-3 top-3 flex items-center gap-1.5 rounded-md px-2 py-0.5"
            style={{ background: "rgba(0,0,0,0.55)" }}
          >
            <span className="text-[9px] font-semibold tracking-[0.12em] text-white/85">FILM</span>
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

      {/* Draggable splitter — drag right to expand film, left to shrink */}
      <div
        onMouseDown={onSplitterDown}
        className="group relative flex-shrink-0 cursor-col-resize select-none"
        style={{ width: 6, marginLeft: 10, marginRight: 10 }}
        aria-label="Drag to resize video and text panels"
        role="separator"
        aria-orientation="vertical"
      >
        <div className="absolute inset-y-0 left-1/2 w-[2px] -translate-x-1/2 rounded-full transition-colors duration-200" style={{ background: "rgba(255,255,255,0.08)" }} />
        <div className="absolute inset-y-0 left-1/2 w-[2px] -translate-x-1/2 rounded-full transition-colors duration-200 group-hover:bg-kinora-gold/40" style={{ background: "transparent" }} />
        {/* Grip dots */}
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 flex flex-col gap-[3px]">
          {[0, 1, 2, 3, 4].map((i) => (
            <div key={i} className="h-[3px] w-[3px] rounded-full" style={{ background: "rgba(255,255,255,0.18)" }} />
          ))}
        </div>
      </div>

      {/* Scrolling book text + reading-progress rail — now on the RIGHT */}
      <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        <div
          ref={scrollRef}
          tabIndex={0}
          data-reading-scroll
          data-testid="reading-scroll"
          aria-label="Reading text — use arrow keys, space, or Page Up/Down to scroll"
          className="scrollbar-slim min-h-0 flex-1 overflow-y-auto pr-6 focus:outline-none"
        >
          <p className="mb-1 text-[10px] font-medium uppercase tracking-[0.2em] text-kinora-muted">
            Now Reading
          </p>
          <h1 className="mb-1 font-serif text-2xl font-semibold text-kinora-text">
            {book.title}
          </h1>
          <p className="mb-7 text-[13px] text-kinora-muted">by {book.author}</p>
          <div className="pb-[40vh]">
            <div
              className="mx-auto"
              data-reading-content
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
                      color: `rgba(${theme.ink}, ${focusOpacity(i)})`,
                      fontSize: `${15 * prefs.fontScale}px`,
                      lineHeight: prefs.leading,
                      letterSpacing: sp.letter,
                      wordSpacing: sp.word,
                      transition: reduce ? "none" : "color 0.25s ease",
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
          <div ref={railFillRef} className="absolute inset-x-0 top-0 rounded-full" style={{ height: "0%", background: "rgba(232,226,216,0.35)" }} />
          {live && (
            <div
              ref={railLeadRef}
              className="absolute inset-x-0 rounded-full"
              style={{
                top: "0%",
                height: `${Math.min(0.18, Math.max(0, (bufferAhead ?? 0) / 30)) * 100}%`,
                background: "rgba(232,226,216,0.15)",
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
          <div ref={railDotRef} className="absolute left-1/2 h-2 w-2 -translate-x-1/2 rounded-full" style={{ top: "0%", background: "rgba(232,226,216,0.8)" }} />
        </div>
      </div>
    </div>
  );
}

export default ScrollFilmEngine;
