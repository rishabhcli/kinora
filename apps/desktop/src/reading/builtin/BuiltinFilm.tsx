// Built-in stand-in for Agent 2's <ScrollFilmEngine> (slot contract in slots.ts).
// The room is fully functional with this today; Agent 12 swaps in the real engine
// at integration. Owns: the vertical film (never-black crossfade), the scrolling
// text column, scroll→focus-word→scheduler wiring, resume, and the active-line lit.
import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { api } from "../../lib/api";
import { READING_THEMES, READING_SPACINGS, type ReadingTheme } from "../../lib/readingPrefs";
import { markReady, promote, pushSrc, type Layer } from "../crossfade";
import { PLACEHOLDER_PARAGRAPHS, pickActiveShot, resolveFilmSrc } from "../fallback";
import type { ScrollFilmEngineProps } from "../slots";

const RESUME_KEY = (id: string) => `kinora.read.${id}`;
const SCROLL_THROTTLE_MS = 160;

/** autoNight resolves to Night between 19:00–07:00 (matches lib/readingPrefs). */
function effectiveThemeOf(prefs: ScrollFilmEngineProps["prefs"]): ReadingTheme {
  const hour = new Date().getHours();
  return prefs.autoNight && (hour >= 19 || hour < 7) ? "night" : prefs.theme;
}

export function BuiltinScrollFilmEngine({
  book,
  pages,
  shots,
  sessionId,
  clipByShot,
  fallbackFilm,
  live,
  prefs,
  reduce,
  onProgress,
  onFirstFrame,
}: ScrollFilmEngineProps) {
  const [focusWord, setFocusWord] = useState(0);
  const [activePara, setActivePara] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const restoredRef = useRef(false);
  const lastIntent = useRef<{ w: number; t: number }>({ w: 0, t: Date.now() });
  const shownFrame = useRef(false);
  const totalWords =
    shots.length && shots[shots.length - 1].source_span
      ? Math.max(1, shots[shots.length - 1].source_span!.word_range[1])
      : 1;

  // Resume where you left off once the text is tall enough to scroll.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || restoredRef.current) return;
    if (el.scrollHeight <= el.clientHeight + 20) return; // not scrollable yet
    let saved = 0;
    try {
      saved = parseFloat(localStorage.getItem(RESUME_KEY(book.id)) || "0");
    } catch {
      /* storage blocked */
    }
    if (saved > 0.02) {
      el.scrollTo({ top: saved * (el.scrollHeight - el.clientHeight) });
      onProgress?.(saved, Math.round(saved * totalWords));
    }
    restoredRef.current = true;
  }, [book.id, pages.length, live, totalWords, onProgress]);

  // Scroll → focus word + scheduler intent + active paragraph + progress (throttled).
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const max = Math.max(1, el.scrollHeight - el.clientHeight);
    const frac = Math.min(1, el.scrollTop / max);
    const w = Math.round(frac * totalWords);
    setFocusWord(w);
    onProgress?.(frac, w);

    if (restoredRef.current) {
      try {
        localStorage.setItem(RESUME_KEY(book.id), String(frac));
      } catch {
        /* storage blocked */
      }
    }

    // Lit paragraph = the one centred in the viewport.
    const cRect = el.getBoundingClientRect();
    const focusY = cRect.top + cRect.height * 0.4;
    let best = 0;
    el.querySelectorAll<HTMLElement>("[data-para]").forEach((p, i) => {
      if (p.getBoundingClientRect().top <= focusY) best = i;
    });
    setActivePara(best);

    // Tell the scheduler where we are (live only). Big jump = seek.
    if (live && sessionId) {
      const now = Date.now();
      const dt = (now - lastIntent.current.t) / 1000;
      const dw = w - lastIntent.current.w;
      if (dt > 0) {
        const vel = Math.min(12, Math.max(2, Math.abs(dw) / dt || 4));
        if (Math.abs(dw) > 120) api.seek(sessionId, w).catch(() => {});
        else api.postIntent(sessionId, w, vel).catch(() => {});
      }
      lastIntent.current = { w, t: now };
    }
  }, [book.id, live, sessionId, totalWords, onProgress]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    let t = 0;
    const handler = () => {
      const now = Date.now();
      if (now - t < SCROLL_THROTTLE_MS) return;
      t = now;
      onScroll();
    };
    el.addEventListener("scroll", handler, { passive: true });
    return () => el.removeEventListener("scroll", handler);
  }, [onScroll]);

  const activeShot = pickActiveShot(shots, focusWord);
  const activeClip = activeShot ? clipByShot[activeShot.shot_id] : undefined;
  const { src, generating } = resolveFilmSrc({
    live,
    activeClip,
    fallbackFilm,
    hasShownFrame: shownFrame.current,
  });

  const effTheme = effectiveThemeOf(prefs);
  const theme = READING_THEMES[effTheme];
  const sp = READING_SPACINGS[prefs.spacing];
  const paragraphs = live && pages.length ? pages.map((p) => p.text) : [...PLACEHOLDER_PARAGRAPHS];

  return (
    <div className="mx-auto flex w-full max-w-[1180px] flex-1 items-stretch gap-10 overflow-hidden px-8 py-8">
      {/* Pinned vertical film */}
      <div className="flex-shrink-0 self-start">
        <div
          className="glass-card relative overflow-hidden rounded-[24px]"
          style={{ width: 320, aspectRatio: "9 / 16", boxShadow: "0 28px 70px -18px rgba(0,0,0,0.7)" } as CSSProperties}
        >
          <CrossfadeFilm
            src={src}
            poster={book.coverImage}
            reduce={reduce}
            generating={generating}
            onShown={() => {
              shownFrame.current = true;
              onFirstFrame?.();
            }}
          />
          <div
            className="absolute left-3 top-3 flex items-center gap-1.5 rounded-full px-2.5 py-1"
            style={{ background: "rgba(0,0,0,0.42)", backdropFilter: "blur(10px)" }}
          >
            <span className="inline-flex h-1.5 w-1.5 rounded-full" style={{ background: "#34d399", boxShadow: "0 0 6px #34d399" }} />
            <span className="text-[9px] font-semibold tracking-wide text-white/90">AI FILM</span>
          </div>
        </div>
        <p className="mt-2.5 text-center text-[10px] text-kinora-muted">
          {live ? "Generated as you read · Wan" : "Generated with Wan · vertical short film"}
        </p>
      </div>

      {/* Scrolling book text */}
      <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        <div
          ref={scrollRef}
          data-reading-scroll
          tabIndex={0}
          aria-label="Reading text — use arrow keys, space, or Page Up/Down to scroll"
          className="hide-scrollbar min-h-0 flex-1 overflow-y-auto pr-6 focus:outline-none"
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
                boxShadow: theme.panel && effTheme !== "night" ? "0 24px 70px -28px rgba(0,0,0,0.7)" : undefined,
                transition: reduce ? "none" : "background 0.3s ease, color 0.3s ease",
              }}
            >
              <div className="space-y-5">
                {paragraphs.map((para, i) => {
                  const active = i === activePara;
                  return (
                    <p
                      key={i}
                      data-para={i}
                      className="font-serif"
                      style={{
                        color: `rgba(${theme.ink}, ${active ? 1 : 0.62})`,
                        borderLeft: `2px solid ${active ? "rgba(212,164,78,0.7)" : "transparent"}`,
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
                  );
                })}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/** Crossfades shot clips so the film never hard-cuts to black (opacity only).
 *  Calls onShown once the first frame is paintable. */
function CrossfadeFilm({
  src,
  poster,
  reduce,
  generating,
  onShown,
}: {
  src: string;
  poster?: string;
  reduce: boolean;
  generating: boolean;
  onShown?: () => void;
}) {
  const [layers, setLayers] = useState<Layer[]>([]);
  const keyRef = useRef(0);
  const shownRef = useRef(false);

  useEffect(() => {
    if (!src) return; // generating — hold the current frame on screen
    const key = ++keyRef.current; // capture atomically so two layers never collide
    setLayers((prev) => pushSrc(prev, src, key));
  }, [src]);

  const fireShown = () => {
    if (shownRef.current) return;
    shownRef.current = true;
    onShown?.();
  };

  if (layers.length === 0) {
    return generating ? (
      <div className="absolute inset-0 grid place-items-center bg-black/60">
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-white/15 border-t-white/60" />
          <p className="text-[11px] text-white/60">Generating your film…</p>
        </div>
      </div>
    ) : null;
  }

  return (
    <>
      {layers.map((l, i) => (
        <video
          key={l.key}
          src={l.src}
          poster={poster}
          autoPlay
          muted
          loop
          playsInline
          onLoadedData={() => {
            fireShown();
            setLayers((prev) => markReady(prev, l.key, reduce));
          }}
          onTransitionEnd={(e) => {
            if (e.propertyName === "opacity" && i === 1) setLayers((prev) => promote(prev, l.key));
          }}
          className="absolute inset-0 h-full w-full bg-black object-cover"
          style={{
            opacity: i === 0 || l.ready ? 1 : 0,
            transition: reduce ? "none" : "opacity 0.55s ease",
            zIndex: i,
          }}
        />
      ))}
    </>
  );
}
