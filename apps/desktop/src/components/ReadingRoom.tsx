import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useState, useEffect, useRef, useCallback, type CSSProperties } from "react";
import type { Book } from "../data/books";
import {
  api,
  toBrowserUrl,
  type ShotResponse,
  type SessionEvent,
  type ClipReady,
  type BufferState,
} from "../lib/api";
import {
  useReadingPrefs,
  READING_THEMES,
  READING_SPACINGS,
  clampPref,
  type ReadingTheme,
} from "../lib/readingPrefs";

const SETTLE: [number, number, number, number] = [0.22, 1, 0.36, 1];
const HINGE: [number, number, number, number] = [0.66, 0, 0.2, 1];

// Bundled real Wan films — the fallback for mock-catalogue books with no backend.
const FALLBACK_FILMS = [
  "/generated/film-01.mp4",
  "/generated/film-02.mp4",
  "/generated/film-03.mp4",
  "/generated/film-04.mp4",
];

interface PageText {
  n: number;
  text: string;
}

/** The reading room. For a real backend book it drives a reading *session*: as
 *  you scroll, it tells the scheduler where you are (focus word + velocity), the
 *  scheduler generates the film a window ahead, and clips swap in over SSE.
 *  Mock catalogue books fall back to a bundled film + placeholder text. */
export default function ReadingRoom({ book, onClose }: { book: Book | null; onClose: () => void }) {
  const [pages, setPages] = useState<PageText[]>([]);
  const [shots, setShots] = useState<ShotResponse[]>([]);
  const [clipByShot, setClipByShot] = useState<Record<string, string>>({});
  const [focusWord, setFocusWord] = useState(0);
  const [bufferAhead, setBufferAhead] = useState<number | null>(null);
  const [bursting, setBursting] = useState(false);
  const [live, setLive] = useState(false);
  const [activePara, setActivePara] = useState(0);
  const [progress, setProgress] = useState(0);
  const reduce = useReducedMotion();
  const [showPrefs, setShowPrefs] = useState(false);
  const { prefs, update, effectiveTheme } = useReadingPrefs();

  const totalWords = useRef(1);
  const sessionId = useRef<string | null>(null);
  const last = useRef<{ w: number; t: number }>({ w: 0, t: Date.now() });
  const scrollRef = useRef<HTMLDivElement>(null);
  const restoredRef = useRef(false);

  const fallbackFilm = book
    ? FALLBACK_FILMS[[...book.id].reduce((a, c) => a + c.charCodeAt(0), 0) % FALLBACK_FILMS.length]
    : FALLBACK_FILMS[0];

  const placeholder = [
    "The first page felt heavy in her hands, as if the weight of every possible life pressed against her fingertips.",
    "Each book was a door, and each door led to a different version of the story — paths not taken, words not yet spoken.",
    "As the pages turned, the world rearranged itself a few seconds ahead, the way a film assembles just before you arrive.",
  ];

  // Load the book's text + shots and open a live reading session (backend only).
  useEffect(() => {
    if (!book || !api.isAuthed()) {
      setLive(false);
      return;
    }
    let alive = true;
    let closeEvents: (() => void) | null = null;
    setPages([]);
    setShots([]);
    setClipByShot({});
    setFocusWord(0);
    setLive(false);

    (async () => {
      try {
        const meta = await api.getBook(book.id); // 404 for a mock book → fallback
        const np = Math.min(meta.num_pages ?? 1, 60);
        const ps: PageText[] = [];
        for (let n = 1; n <= np; n++) {
          try {
            const p = await api.getPage(book.id, n);
            if (p.text) ps.push({ n, text: p.text });
          } catch {
            /* page not rendered */
          }
        }
        const sh = (await api.getShots(book.id))
          .filter((s) => s.source_span)
          .sort((a, b) => a.source_span!.word_range[0] - b.source_span!.word_range[0]);
        if (!alive) return;
        if (ps.length === 0 || sh.length === 0) {
          setLive(false);
          return;
        }
        setPages(ps);
        setShots(sh);
        totalWords.current = Math.max(1, sh[sh.length - 1].source_span!.word_range[1]);
        const seed: Record<string, string> = {};
        for (const s of sh) if (s.clip_url) seed[s.shot_id] = toBrowserUrl(s.clip_url);
        setClipByShot(seed);
        setLive(true);

        const sess = await api.createSession(book.id, 0);
        sessionId.current = sess.session_id;
        closeEvents = api.openSessionEvents(sess.session_id, (e: SessionEvent) => {
          if (e.event === "clip_ready") {
            const c = e as ClipReady;
            if (c.oss_url) setClipByShot((m) => ({ ...m, [c.shot_id]: toBrowserUrl(c.oss_url) }));
          } else if (e.event === "buffer_state") {
            const b = e as BufferState;
            setBufferAhead(b.committed_seconds_ahead ?? null);
            setBursting(Boolean(b.bursting));
          }
        });
        api.postIntent(sess.session_id, 0, 4).catch(() => {}); // prime the scheduler
      } catch {
        if (alive) setLive(false);
      }
    })();

    return () => {
      alive = false;
      closeEvents?.();
      sessionId.current = null;
    };
  }, [book]);

  // Lock the page behind; close on Escape; move focus into the reader (so the
  // text region is keyboard-scrollable) and restore it on close.
  useEffect(() => {
    if (!book) return;
    restoredRef.current = false;
    setActivePara(0);
    setProgress(0);
    setShowPrefs(false);
    scrollRef.current?.scrollTo({ top: 0 });
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const prevFocus = document.activeElement as HTMLElement | null;
    const focusTimer = window.setTimeout(() => scrollRef.current?.focus(), 480);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prevOverflow;
      document.removeEventListener("keydown", onKey);
      window.clearTimeout(focusTimer);
      prevFocus?.focus?.();
    };
    // onClose is referenced via closure (its behaviour is stable); keying on it
    // would re-run focus handling on every parent render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [book]);

  // Resume where you left off: once the text is tall enough to scroll, jump to
  // the saved fraction for this book (persisted per-book in localStorage).
  useEffect(() => {
    if (!book) return;
    const el = scrollRef.current;
    if (!el || restoredRef.current) return;
    if (el.scrollHeight <= el.clientHeight + 20) return; // not scrollable yet
    let saved = 0;
    try {
      saved = parseFloat(localStorage.getItem("kinora.read." + book.id) || "0");
    } catch {
      /* storage blocked */
    }
    if (saved > 0.02) {
      el.scrollTo({ top: saved * (el.scrollHeight - el.clientHeight) });
      setProgress(saved);
    }
    restoredRef.current = true; // from here on, scrolling persists progress
  }, [book, pages.length, live]);

  // Scroll → focus word → tell the scheduler (throttled). Big jumps = seek.
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el || !live) return;
    const frac = el.scrollTop / Math.max(1, el.scrollHeight - el.clientHeight);
    const w = Math.round(frac * totalWords.current);
    setFocusWord(w);
    const now = Date.now();
    const dt = (now - last.current.t) / 1000;
    const dw = w - last.current.w;
    const sid = sessionId.current;
    if (sid && dt > 0) {
      const vel = Math.min(12, Math.max(2, Math.abs(dw) / dt || 4));
      if (Math.abs(dw) > 120) api.seek(sid, w).catch(() => {});
      else api.postIntent(sid, w, vel).catch(() => {});
    }
    last.current = { w, t: now };
  }, [live]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    let t = 0;
    const onScroll = () => {
      const now = Date.now();
      if (now - t < 160) return;
      t = now;
      // Reading focus: brighten the paragraph centred in the viewport. Driven by
      // the same scroll position as the film, so the lit passage is the one the
      // film is showing. Works for both live and placeholder text.
      const el2 = scrollRef.current;
      if (el2) {
        const max = Math.max(1, el2.scrollHeight - el2.clientHeight);
        setProgress(Math.min(1, el2.scrollTop / max));
        if (restoredRef.current && book) {
          try {
            localStorage.setItem("kinora.read." + book.id, String(el2.scrollTop / max));
          } catch {
            /* storage blocked */
          }
        }
        const cRect = el2.getBoundingClientRect();
        const focusY = cRect.top + cRect.height * 0.4;
        let best = 0;
        el2.querySelectorAll<HTMLElement>("[data-para]").forEach((p, i) => {
          if (p.getBoundingClientRect().top <= focusY) best = i;
        });
        setActivePara(best);
      }
      handleScroll();
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [handleScroll, pages.length, book]);

  // Active shot = greatest word_range start ≤ focus word; play its clip.
  let activeShot: ShotResponse | null = null;
  for (const s of shots) {
    if (s.source_span && s.source_span.word_range[0] <= focusWord) activeShot = s;
    else break;
  }
  const activeClip = activeShot ? clipByShot[activeShot.shot_id] : undefined;
  const film = live ? activeClip ?? "" : fallbackFilm;
  const generating = live && !activeClip;
  const theme = READING_THEMES[effectiveTheme];
  const sp = READING_SPACINGS[prefs.spacing];
  const reduceTransparency =
    typeof window !== "undefined" &&
    !!window.matchMedia?.("(prefers-reduced-transparency: reduce)").matches;

  return (
    <AnimatePresence>
      {book && (
        <motion.div
          className="fixed inset-0 z-[100]"
          role="dialog"
          aria-modal="true"
          aria-label={`Reading ${book.title}`}
          initial="closed"
          animate="open"
          exit="closed"
        >
          <motion.div
            className="absolute inset-0"
            onClick={onClose}
            variants={{
              closed: { backgroundColor: "rgba(8,7,6,0)" },
              open: { backgroundColor: "rgba(8,7,6,0.78)" },
            }}
            transition={{ duration: 0.6, ease: SETTLE }}
            // Static blur, not animated — animating backdrop-filter is a GPU
            // hotspot. Dropped under prefers-reduced-transparency.
            style={reduceTransparency ? undefined : { backdropFilter: "blur(20px)", WebkitBackdropFilter: "blur(20px)" }}
          />

          <motion.div
            className="absolute inset-0 flex flex-col kinora-bg"
            variants={{
              closed: { opacity: 0, scale: 1.06 },
              open: { opacity: 1, scale: 1 },
            }}
            transition={{ duration: 0.6, ease: SETTLE, delay: 0.42 }}
            // .kinora-bg sets position:relative, which overrides Tailwind's
            // `absolute` (same specificity, later in source) — force it back so
            // inset-0 fills the viewport and the reading column can bound + scroll.
            style={{ transformOrigin: "center", position: "absolute" }}
          >
            {/* Top bar */}
            <div className="flex items-center gap-3 px-6 py-3 flex-shrink-0" style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
              <button onClick={onClose} className="glass-control flex items-center gap-2 px-3 py-1.5 rounded-lg text-[12px] font-medium">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><path d="M15 18l-6-6 6-6" /></svg>
                Back
              </button>
              <span className="font-serif text-sm font-semibold text-kinora-text">{book.title}</span>
              <span className="text-[11px] text-kinora-muted">· {book.author}</span>
              <div className="flex-1" />

              {/* Reading appearance controls (theme + typography + comfort) */}
              <div className="relative">
                <button
                  onClick={() => setShowPrefs((v) => !v)}
                  aria-label="Reading settings"
                  aria-expanded={showPrefs}
                  className="glass-control flex items-baseline gap-0.5 rounded-lg px-2.5 py-1.5 font-serif text-kinora-text"
                >
                  <span className="text-[11px]">A</span>
                  <span className="text-[15px] leading-none">A</span>
                </button>
                {showPrefs && (
                  <div className="glass-card absolute right-0 top-11 z-30 w-60 rounded-xl p-3.5" style={{ background: "rgba(20,18,16,0.97)" }}>
                    <p className="mb-1.5 text-[10px] uppercase tracking-wider text-kinora-muted">Theme</p>
                    <div className="mb-3 flex gap-2.5">
                      {(Object.keys(READING_THEMES) as ReadingTheme[]).map((t) => (
                        <button
                          key={t}
                          onClick={() => update({ theme: t })}
                          title={READING_THEMES[t].label}
                          aria-label={READING_THEMES[t].label}
                          className="h-7 w-7 rounded-full transition-transform hover:scale-110"
                          style={{ background: READING_THEMES[t].swatch, border: `2px solid ${prefs.theme === t ? "rgba(212,164,78,0.95)" : "rgba(255,255,255,0.18)"}` }}
                        />
                      ))}
                    </div>
                    <label className="mb-3 flex items-center justify-between text-[11px] text-kinora-text/85">
                      <span>Auto night</span>
                      <input type="checkbox" checked={prefs.autoNight} onChange={(e) => update({ autoNight: e.target.checked })} />
                    </label>
                    <div style={{ height: 1, background: "rgba(255,255,255,0.08)", marginBottom: 10 }} />
                    <PrefStepper label="Text size" value={`${Math.round(prefs.fontScale * 100)}%`}
                      onMinus={() => update({ fontScale: clampPref(+(prefs.fontScale - 0.1).toFixed(2), 0.85, 1.5) })}
                      onPlus={() => update({ fontScale: clampPref(+(prefs.fontScale + 0.1).toFixed(2), 0.85, 1.5) })} />
                    <PrefStepper label="Line spacing" value={prefs.leading.toFixed(1)}
                      onMinus={() => update({ leading: clampPref(+(prefs.leading - 0.1).toFixed(1), 1.4, 2.2) })}
                      onPlus={() => update({ leading: clampPref(+(prefs.leading + 0.1).toFixed(1), 1.4, 2.2) })} />
                    <PrefStepper label="Width" value={`${prefs.measure}ch`}
                      onMinus={() => update({ measure: clampPref(prefs.measure - 4, 48, 80) })}
                      onPlus={() => update({ measure: clampPref(prefs.measure + 4, 48, 80) })} />
                    <button
                      onClick={() => update({ spacing: prefs.spacing === "normal" ? "relaxed" : prefs.spacing === "relaxed" ? "loose" : "normal" })}
                      className="mt-1 flex w-full items-center justify-between rounded-md px-2 py-1.5 text-[11px] text-kinora-text/85"
                      style={{ background: "rgba(255,255,255,0.06)" }}
                    >
                      <span>Letter spacing</span>
                      <span className="text-kinora-muted">{READING_SPACINGS[prefs.spacing].label}</span>
                    </button>
                  </div>
                )}
              </div>

              {live && (
                <div aria-live="polite" className="flex items-center gap-2 rounded-full px-3 py-1 text-[10px] font-medium" style={{ background: "rgba(0,0,0,0.4)", border: "0.5px solid rgba(255,255,255,0.12)" }}>
                  <span className="inline-flex h-1.5 w-1.5 rounded-full" style={{ background: bursting ? "#fbbf24" : "#34d399", boxShadow: `0 0 6px ${bursting ? "#fbbf24" : "#34d399"}` }} />
                  <span className="text-white/80">{generating ? "Generating ahead…" : `Buffered ${Math.round(bufferAhead ?? 0)}s ahead`}</span>
                </div>
              )}
            </div>

            <div className="mx-auto flex w-full max-w-[1180px] flex-1 items-stretch gap-10 overflow-hidden px-8 py-8">
              {/* Pinned vertical film */}
              <div className="flex-shrink-0 self-start">
                <div className="glass-card relative overflow-hidden rounded-[24px]" style={{ width: 320, aspectRatio: "9 / 16", boxShadow: "0 28px 70px -18px rgba(0,0,0,0.7)" } as CSSProperties}>
                  <CrossfadeFilm src={film} poster={book.coverImage} reduce={!!reduce} generating={generating} />
                  <div className="absolute left-3 top-3 flex items-center gap-1.5 rounded-full px-2.5 py-1" style={{ background: "rgba(0,0,0,0.42)", backdropFilter: "blur(10px)" }}>
                    <span className="inline-flex h-1.5 w-1.5 rounded-full" style={{ background: "#34d399", boxShadow: "0 0 6px #34d399" }} />
                    <span className="text-[9px] font-semibold tracking-wide text-white/90">AI FILM</span>
                  </div>
                </div>
                <p className="mt-2.5 text-center text-[10px] text-kinora-muted">
                  {live ? "Generated as you read · Wan" : "Generated with Wan · vertical short film"}
                </p>
              </div>

              {/* Scrolling book text + reading-progress rail */}
              <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
                <div ref={scrollRef} tabIndex={0} aria-label="Reading text — use arrow keys, space, or Page Up/Down to scroll" className="hide-scrollbar min-h-0 flex-1 overflow-y-auto pr-6 focus:outline-none">
                  <p className="text-[10px] text-kinora-muted uppercase tracking-widest mb-2">Now Reading</p>
                  <h1 className="font-serif text-2xl font-semibold text-kinora-text mb-1">{book.title}</h1>
                  <p className="text-[13px] text-kinora-muted mb-7">by {book.author}</p>
                  <div className="pb-[40vh]">
                    <div
                      className="mx-auto"
                      style={{
                        maxWidth: `${prefs.measure}ch`,
                        background: theme.pageBg,
                        color: `rgb(${theme.ink})`,
                        borderRadius: theme.panel ? 16 : 0,
                        padding: theme.panel ? "30px 34px" : 0,
                        boxShadow: theme.panel && effectiveTheme !== "night" ? "0 24px 70px -28px rgba(0,0,0,0.7)" : undefined,
                        transition: reduce ? "none" : "background 0.3s ease, color 0.3s ease",
                      }}
                    >
                      <div className="space-y-5">
                        {(live ? pages.map((p) => p.text) : placeholder).map((para, i) => {
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

                {/* Reading-progress + buffer-ahead rail: filled = read so far,
                    glowing lead = seconds of film already committed ahead of you,
                    ticks = shots, dot = your place. */}
                <div className="pointer-events-none absolute right-1 top-1 bottom-1 w-1 rounded-full" aria-hidden style={{ background: "rgba(255,255,255,0.06)" }}>
                  <div className="absolute inset-x-0 top-0 rounded-full" style={{ height: `${progress * 100}%`, background: "rgba(212,164,78,0.55)", transition: reduce ? "none" : "height 0.2s linear" }} />
                  {live && (
                    <div
                      className="absolute inset-x-0 rounded-full"
                      style={{
                        top: `${progress * 100}%`,
                        height: `${Math.min(0.18, Math.max(0, (bufferAhead ?? 0) / 30)) * 100}%`,
                        background: `linear-gradient(180deg, ${bursting ? "rgba(251,191,36,0.85)" : "rgba(52,211,153,0.75)"}, transparent)`,
                        boxShadow: `0 0 8px ${bursting ? "rgba(251,191,36,0.55)" : "rgba(52,211,153,0.45)"}`,
                        transition: reduce ? "none" : "top 0.2s linear, height 0.4s ease",
                      }}
                    />
                  )}
                  {live &&
                    shots.map((s) =>
                      s.source_span ? (
                        <div
                          key={s.shot_id}
                          className="absolute left-1/2 h-[2px] w-[7px] -translate-x-1/2 rounded-full"
                          style={{ top: `${(s.source_span.word_range[0] / totalWords.current) * 100}%`, background: "rgba(255,255,255,0.22)" }}
                        />
                      ) : null
                    )}
                  <div
                    className="absolute left-1/2 h-2 w-2 -translate-x-1/2 rounded-full"
                    style={{ top: `calc(${progress * 100}% - 4px)`, background: "#e8e2d8", boxShadow: "0 0 6px rgba(232,226,216,0.7)", transition: reduce ? "none" : "top 0.2s linear" }}
                  />
                </div>
              </div>
            </div>
          </motion.div>

          {/* Cover swings open on its spine, then lifts away. */}
          <div className="absolute inset-0 flex items-center justify-center pointer-events-none" style={{ perspective: 2200 } as CSSProperties}>
            <motion.div
              className="relative"
              style={{ width: "min(40vh, 300px)", aspectRatio: "2 / 3", transformStyle: "preserve-3d", transformOrigin: "left center" } as CSSProperties}
              variants={{ closed: { rotateY: 0, opacity: 1 }, open: { rotateY: -168, opacity: 0 } }}
              transition={{ rotateY: { duration: 0.95, ease: HINGE, delay: 0.12 }, opacity: { duration: 0.25, ease: "linear", delay: 0.95 } }}
            >
              <div className="absolute inset-0 overflow-hidden" style={{ background: book.coverGradient, borderRadius: "3px 8px 8px 3px", backfaceVisibility: "hidden", boxShadow: "0 30px 60px -20px rgba(0,0,0,0.85)" }}>
                <img src={book.coverImage} alt="" className="absolute inset-0 h-full w-full object-cover" onError={(e) => ((e.target as HTMLImageElement).style.display = "none")} />
                <div className="absolute inset-y-0 left-0" style={{ width: 14, background: "linear-gradient(90deg, rgba(0,0,0,0.4), transparent)" }} />
              </div>
            </motion.div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/** Crossfades between shot clips so the film never hard-cuts to black: the
 *  current clip stays visible until the next one decodes, then they cross-fade
 *  (opacity only — GPU-cheap). Reduced motion swaps instantly. Caps at two
 *  concurrent <video> elements. */
function CrossfadeFilm({
  src,
  poster,
  reduce,
  generating,
}: {
  src: string;
  poster?: string;
  reduce: boolean;
  generating: boolean;
}) {
  type Layer = { key: number; src: string; ready: boolean };
  const [layers, setLayers] = useState<Layer[]>([]);
  const keyRef = useRef(0);

  useEffect(() => {
    if (!src) return; // generating the next clip — keep the last frame on screen
    setLayers((prev) => {
      if (prev.length === 0) return [{ key: keyRef.current++, src, ready: false }];
      const base = prev[0];
      if (base.src === src) return [base]; // scrolled back to the visible clip — drop incoming
      return [base, { key: keyRef.current++, src, ready: false }]; // base stays until new fades in
    });
  }, [src]);

  const markReady = (key: number) =>
    setLayers((prev) => {
      const i = prev.findIndex((l) => l.key === key);
      if (i === -1) return prev;
      const next = prev.map((l) => (l.key === key ? { ...l, ready: true } : l));
      if (reduce && i === 1) return [next[1]]; // no fade → promote instantly
      return next;
    });

  const promote = (key: number) =>
    setLayers((prev) => (prev.length === 2 && prev[1].key === key ? [prev[1]] : prev));

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
          onCanPlay={() => markReady(l.key)}
          onTransitionEnd={(e) => {
            if (e.propertyName === "opacity" && i === 1) promote(l.key);
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

/** A compact −/value/+ row for the reading-settings popover. */
function PrefStepper({
  label,
  value,
  onMinus,
  onPlus,
}: {
  label: string;
  value: string;
  onMinus: () => void;
  onPlus: () => void;
}) {
  const btn = "grid h-6 w-6 place-items-center rounded-md text-kinora-text";
  return (
    <div className="mb-2 flex items-center justify-between">
      <span className="text-[11px] text-kinora-text/85">{label}</span>
      <div className="flex items-center gap-2">
        <button onClick={onMinus} aria-label={`Decrease ${label}`} className={btn} style={{ background: "rgba(255,255,255,0.08)" }}>−</button>
        <span className="w-11 text-center text-[11px] text-kinora-muted">{value}</span>
        <button onClick={onPlus} aria-label={`Increase ${label}`} className={btn} style={{ background: "rgba(255,255,255,0.08)" }}>+</button>
      </div>
    </div>
  );
}
