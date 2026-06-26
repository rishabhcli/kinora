// WS4 — the reading-room shell. Composes Agent 2's <ScrollFilmEngine> with the
// top bar and warm-up overlay; owns the chrome: Escape-to-close, body-scroll
// lock, a focus trap, and focus-into-the-reader on reveal.
import { AnimatePresence } from "framer-motion";
import { useCallback, useEffect, useRef, type Dispatch } from "react";
import type { Book } from "../data/books";
import { useReadingPrefs } from "../lib/readingPrefs";
import { ScrollFilmEngine } from "./producers";
import { WarmUp } from "./WarmUp";
import type { FilmSession } from "./useFilmSession";
import type { MachineEvent, MachineState } from "./machine";

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
  const { prefs } = useReadingPrefs();

  useRoomChrome(onClose, rootRef);

  // Once revealed, move keyboard focus into the reading text so arrow keys scroll.
  useEffect(() => {
    if (state.phase !== "reading") return;
    const t = window.setTimeout(() => {
      rootRef.current?.querySelector<HTMLElement>("[data-reading-scroll]")?.focus();
    }, 60);
    return () => window.clearTimeout(t);
  }, [state.phase]);

  const onFirstFrame = useCallback(() => dispatch({ type: "FIRST_FRAME" }), [dispatch]);

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

      {/* Content area — film engine + warm-up overlay */}
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
          onFirstFrame={onFirstFrame}
        />

        {/* Warm-up overlay — fades out the instant the film is revealed */}
        <AnimatePresence>
          {showWarmUp && <WarmUp key="warmup" state={state} session={session} bookTitle={book.title} reduce={reduce} />}
        </AnimatePresence>
      </div>
    </div>
  );
}
