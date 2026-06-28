// WS4 — the reading-room shell. Composes Agent 2's <ScrollFilmEngine> + Agent 6's
// <ReadingControls> (via producers.tsx) with the top bar, the progress/buffer
// rail, and the warm-up overlay; owns the chrome: Escape-to-close, body-scroll
// lock, a focus trap, and focus-into-the-reader on reveal.
import { AnimatePresence, motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState, type Dispatch } from "react";
import { useTranslation } from "react-i18next";
import type { Book } from "../data/books";
import { useReadingPrefs } from "../lib/readingPrefs";
import { ScrollFilmEngine, ReadingControls } from "./producers";
import { WarmUp } from "./WarmUp";
import type { FilmSession } from "./useFilmSession";
import type { MachineEvent, MachineState } from "./machine";

const RAIL_SETTLE = "0.2s linear";

/** Body-scroll lock + Escape-to-close + a basic Tab focus trap, with focus
 *  restored to the previously focused element on close. */
function useRoomChrome(onClose: () => void, rootRef: React.RefObject<HTMLDivElement | null>) {
  useEffect(() => {
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const prevFocus = document.activeElement as HTMLElement | null;

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const root = rootRef.current;
      if (!root) return;
      const focusables = Array.from(
        root.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), textarea, select, [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((el) => el.offsetParent !== null || el === document.activeElement);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prevOverflow;
      document.removeEventListener("keydown", onKey);
      prevFocus?.focus?.();
    };
  }, [onClose, rootRef]);
}

export function ReadingRoomShell({
  book,
  onClose,
  state,
  dispatch,
  session,
  reduce,
}: {
  book: Book;
  onClose: () => void;
  state: MachineState;
  dispatch: Dispatch<MachineEvent>;
  session: FilmSession;
  reduce: boolean;
}) {
  const { t } = useTranslation();
  const rootRef = useRef<HTMLDivElement>(null);
  const { prefs, update } = useReadingPrefs();
  const [progress, setProgress] = useState(0);
  const [controlsOpen, setControlsOpen] = useState(false);

  useRoomChrome(onClose, rootRef);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (!target.closest("[data-controls-popover]")) {
        setControlsOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  // Once revealed, move keyboard focus into the reading text so arrow keys scroll.
  useEffect(() => {
    if (state.phase !== "reading") return;
    const t = window.setTimeout(() => {
      rootRef.current?.querySelector<HTMLElement>("[data-reading-scroll]")?.focus();
    }, 60);
    return () => window.clearTimeout(t);
  }, [state.phase]);

  // Stable so the engine's scroll listener isn't re-bound on every re-render.
  const onProgress = useCallback((frac: number) => setProgress(frac), []);

  const totalWords =
    session.shots.length && session.shots[session.shots.length - 1].source_span
      ? Math.max(1, session.shots[session.shots.length - 1].source_span!.word_range[1])
      : 1;

  const showWarmUp = ["opening", "loading", "warming", "ready"].includes(state.phase);
  const pill =
    session.bufferAhead != null && session.bufferAhead > 0.5
      ? t("reading.bufferedAhead", { seconds: Math.round(session.bufferAhead) })
      : t("reading.generatingAhead");

  return (
    <div ref={rootRef} className="flex h-full flex-col" aria-label={t("reading.readingAria", { title: book.title })}>
      {/* Top bar */}
      <div
        className="flex flex-shrink-0 items-center gap-3 px-6 py-3.5"
        style={{
          borderBottom: "1px solid rgba(255,255,255,0.06)",
          background: "linear-gradient(180deg, rgba(255,255,255,0.025) 0%, transparent 100%)",
        }}
      >
        <button
          onClick={onClose}
          aria-label={t("reading.closeReader")}
          className="flex items-center gap-2 rounded-xl px-3.5 py-2 text-[12px] font-semibold transition-all duration-200"
          style={{
            background: "linear-gradient(180deg, #e8c878 0%, #d4a44e 100%)",
            border: "none",
            color: "#0a0908",
            boxShadow:
              "inset 0 1px 0 rgba(246,240,231,0.5), 0 8px 22px -8px rgba(212,164,78,0.5)",
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round">
            <path d="M15 18l-6-6 6-6" />
          </svg>
          {t("reading.back")}
        </button>
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="font-serif text-[15px] font-semibold truncate"
            style={{
              background: "linear-gradient(168deg, #f4efe6 0%, #efe3cf 42%, #e8c878 100%",
              WebkitBackgroundClip: "text",
              backgroundClip: "text",
              WebkitTextFillColor: "transparent",
              color: "transparent",
            }}
          >
            {book.title}
          </span>
          <span className="text-[11px] text-kinora-muted flex-shrink-0">· {book.author}</span>
        </div>
        <div className="flex-1" />

        {/* Settings popover */}
        <div data-controls-popover className="relative">
          <button
            onClick={() => setControlsOpen(!controlsOpen)}
            aria-label={t("reading.readingSettings")}
            className="flex items-center justify-center transition-all duration-200"
            style={{
              width: 34,
              height: 34,
              borderRadius: 10,
              background: controlsOpen
                ? "linear-gradient(135deg, rgba(212,164,78,0.15) 0%, rgba(212,164,78,0.06) 100%)"
                : "rgba(255,255,255,0.04)",
              border: controlsOpen ? "1px solid rgba(212,164,78,0.2)" : "1px solid rgba(255,255,255,0.06)",
              color: controlsOpen ? "#e8c878" : "rgba(232, 226, 216, 0.7)",
              cursor: "pointer",
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
          {controlsOpen && (
            <div
              className="absolute right-0 top-11 z-50 rounded-2xl p-4"
              style={{
                background: "rgba(20, 17, 15, 0.93)",
                border: "0.5px solid rgba(246,240,231,0.16)",
                boxShadow:
                  "inset 0 1px 0 rgba(246,240,231,0.1), 0 34px 90px -22px rgba(6,5,4,0.85)",
                maxHeight: "70vh",
                overflowY: "auto",
                backdropFilter: "blur(36px) saturate(135%)",
                WebkitBackdropFilter: "blur(36px) saturate(135%)",
              }}
            >
              <ReadingControls prefs={prefs} onChange={update} reduce={reduce} />
            </div>
          )}
        </div>

        {session.live && (
          <div
            aria-live="polite"
            className="flex items-center gap-2 rounded-full px-3 py-1.5 text-[10px] font-medium"
            style={{
              background: "linear-gradient(135deg, rgba(52,211,153,0.12) 0%, rgba(52,211,153,0.04) 100%)",
              border: "1px solid rgba(52,211,153,0.15)",
            }}
          >
            <span
              className="inline-flex h-1.5 w-1.5 rounded-full"
              style={{ background: session.bursting ? "#fbbf24" : "#34d399", boxShadow: `0 0 6px ${session.bursting ? "#fbbf24" : "#34d399"}` }}
            />
            <span style={{ color: session.bursting ? "#fbbf24" : "#34d399" }}>{pill}</span>
          </div>
        )}
      </div>

      {/* Content area — film engine + progress rail + warm-up overlay */}
      <div className="relative flex min-h-0 flex-1 flex-col">
        <ScrollFilmEngine
          book={book}
          pages={session.pages}
          shots={session.shots}
          sessionId={session.sessionId}
          clipByShot={session.clipByShot}
          fallbackFilm={session.fallbackFilm}
          live={session.live}
          prefs={prefs}
          reduce={reduce}
          onProgress={onProgress}
        />

        {/* Reading-progress + buffer-ahead rail */}
        <div className="pointer-events-none absolute bottom-6 right-3 top-6 w-1 rounded-full" aria-hidden style={{ background: "rgba(255,255,255,0.06)" }}>
          <div
            className="absolute inset-x-0 top-0 rounded-full"
            style={{ height: `${progress * 100}%`, background: "linear-gradient(180deg, rgba(212,164,78,0.7) 0%, rgba(212,164,78,0.4) 100%)", transition: reduce ? "none" : `height ${RAIL_SETTLE}` }}
          />
          {session.live && (
            <div
              className="absolute inset-x-0 rounded-full"
              style={{
                top: `${progress * 100}%`,
                height: `${Math.min(0.18, Math.max(0, (session.bufferAhead ?? 0) / 30)) * 100}%`,
                background: `linear-gradient(180deg, ${session.bursting ? "rgba(251,191,36,0.85)" : "rgba(52,211,153,0.75)"}, transparent)`,
                boxShadow: `0 0 8px ${session.bursting ? "rgba(251,191,36,0.55)" : "rgba(52,211,153,0.45)"}`,
                transition: reduce ? "none" : `top ${RAIL_SETTLE}, height 0.4s ease`,
              }}
            />
          )}
          {session.live &&
            session.shots.map((s) =>
              s.source_span ? (
                <div
                  key={s.shot_id}
                  className="absolute left-1/2 h-[2px] w-[7px] -translate-x-1/2 rounded-full"
                  style={{ top: `${(s.source_span.word_range[0] / totalWords) * 100}%`, background: "rgba(255,255,255,0.22)" }}
                />
              ) : null,
            )}
          <div
            className="absolute left-1/2 h-2 w-2 -translate-x-1/2 rounded-full"
            style={{ top: `calc(${progress * 100}% - 4px)`, background: "#e8e2d8", boxShadow: "0 0 6px rgba(232,226,216,0.7)", transition: reduce ? "none" : `top ${RAIL_SETTLE}` }}
          />
        </div>

        {/* Warm-up overlay — fades out the instant the film is revealed */}
        <AnimatePresence>
          {showWarmUp && <WarmUp key="warmup" state={state} session={session} bookTitle={book.title} reduce={reduce} />}
        </AnimatePresence>
      </div>
    </div>
  );
}
