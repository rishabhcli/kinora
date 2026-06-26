// WS4 — the reading-room shell. Composes Agent 2's <ScrollFilmEngine> + Agent 6's
// <ReadingControls> (via producers.tsx) with the top bar, the progress/buffer
// rail, and the warm-up overlay; owns the chrome: Escape-to-close, body-scroll
// lock, a focus trap, and focus-into-the-reader on reveal.
import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, useState, type Dispatch } from "react";
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
  const rootRef = useRef<HTMLDivElement>(null);
  const { prefs, update } = useReadingPrefs();
  const [progress, setProgress] = useState(0);

  useRoomChrome(onClose, rootRef);

  // Once revealed, move keyboard focus into the reading text so arrow keys scroll.
  useEffect(() => {
    if (state.phase !== "reading") return;
    const t = window.setTimeout(() => {
      rootRef.current?.querySelector<HTMLElement>("[data-reading-scroll]")?.focus();
    }, 60);
    return () => window.clearTimeout(t);
  }, [state.phase]);

  const totalWords =
    session.shots.length && session.shots[session.shots.length - 1].source_span
      ? Math.max(1, session.shots[session.shots.length - 1].source_span!.word_range[1])
      : 1;

  const showWarmUp = ["opening", "loading", "warming", "ready"].includes(state.phase);
  const pill =
    session.bufferAhead != null && session.bufferAhead > 0.5
      ? `Buffered ${Math.round(session.bufferAhead)}s ahead`
      : "Generating ahead…";

  return (
    <div ref={rootRef} className="flex h-full flex-col" aria-label={`Reading ${book.title}`}>
      {/* Top bar */}
      <div className="flex flex-shrink-0 items-center gap-3 px-6 py-3" style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
        <button onClick={onClose} aria-label="Close reader and go back" className="glass-control flex items-center gap-2 rounded-lg px-3 py-1.5 text-[12px] font-medium">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <path d="M15 18l-6-6 6-6" />
          </svg>
          Back
        </button>
        <span className="font-serif text-sm font-semibold text-kinora-text">{book.title}</span>
        <span className="text-[11px] text-kinora-muted">· {book.author}</span>
        <div className="flex-1" />

        <ReadingControls prefs={prefs} onChange={update} reduce={reduce} />

        {session.live && (
          <div
            aria-live="polite"
            className="flex items-center gap-2 rounded-full px-3 py-1 text-[10px] font-medium"
            style={{ background: "rgba(0,0,0,0.4)", border: "0.5px solid rgba(255,255,255,0.12)" }}
          >
            <span
              className="inline-flex h-1.5 w-1.5 rounded-full"
              style={{ background: session.bursting ? "#fbbf24" : "#34d399", boxShadow: `0 0 6px ${session.bursting ? "#fbbf24" : "#34d399"}` }}
            />
            <span className="text-white/80">{pill}</span>
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
          onProgress={(frac) => setProgress(frac)}
          onFirstFrame={() => dispatch({ type: "FIRST_FRAME" })}
        />

        {/* Reading-progress + buffer-ahead rail */}
        <div className="pointer-events-none absolute bottom-6 right-3 top-6 w-1 rounded-full" aria-hidden style={{ background: "rgba(255,255,255,0.06)" }}>
          <div
            className="absolute inset-x-0 top-0 rounded-full"
            style={{ height: `${progress * 100}%`, background: "rgba(212,164,78,0.55)", transition: reduce ? "none" : `height ${RAIL_SETTLE}` }}
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
