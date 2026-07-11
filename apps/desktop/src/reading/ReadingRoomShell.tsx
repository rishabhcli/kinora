// Minimal reading-room shell: the book, its accepted film, and only the chrome
// needed to leave the room or understand current generation state.
import { AnimatePresence } from "framer-motion";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { Book } from "../data/books";
import { useReadingPrefs } from "../lib/readingPrefs";
import type { MachineState } from "./machine";
import { ScrollFilmEngine } from "./producers";
import type { FilmSession } from "./useFilmSession";
import { WarmUp } from "./WarmUp";

const RAIL_SETTLE = "0.2s linear";

function useRoomChrome(onClose: () => void, rootRef: React.RefObject<HTMLDivElement | null>) {
  useEffect(() => {
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const prevFocus = document.activeElement as HTMLElement | null;

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const root = rootRef.current;
      if (!root) return;
      const focusables = Array.from(
        root.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), textarea, select, [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => element.offsetParent !== null || element === document.activeElement);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
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
  session,
  reduce,
}: {
  book: Book;
  onClose: () => void;
  state: MachineState;
  session: FilmSession;
  reduce: boolean;
}) {
  const { t } = useTranslation();
  const rootRef = useRef<HTMLDivElement>(null);
  const { prefs } = useReadingPrefs();
  const [progress, setProgress] = useState(0);
  useRoomChrome(onClose, rootRef);

  useEffect(() => {
    if (state.phase !== "reading") return;
    const timer = window.setTimeout(() => {
      rootRef.current?.querySelector<HTMLElement>("[data-reading-scroll]")?.focus();
    }, 60);
    return () => window.clearTimeout(timer);
  }, [state.phase]);

  const onProgress = useCallback((fraction: number) => setProgress(fraction), []);
  const totalWords =
    session.shots.length && session.shots[session.shots.length - 1].source_span
      ? Math.max(1, session.shots[session.shots.length - 1].source_span!.word_range[1])
      : 1;
  const showWarmUp = ["opening", "loading", "warming", "ready"].includes(state.phase);
  const status =
    session.bufferAhead != null && session.bufferAhead > 0.5
      ? t("reading.bufferedAhead", { seconds: Math.round(session.bufferAhead) })
      : t("reading.generatingAhead");

  return (
    <div ref={rootRef} className="flex h-full flex-col" aria-label={t("reading.readingAria", { title: book.title })}>
      <div
        className="flex flex-shrink-0 items-center gap-3 px-6 py-3"
        style={{ borderBottom: "1px solid rgba(255,255,255,0.06)", background: "rgba(10,9,8,0.72)" }}
      >
        <button
          onClick={onClose}
          aria-label={t("reading.closeReader")}
          title={t("reading.back")}
          className="grid h-8 w-8 place-items-center rounded-md text-kinora-muted transition-colors hover:bg-white/[0.05] hover:text-kinora-text"
          style={{ border: "1px solid rgba(255,255,255,0.06)" }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round">
            <path d="M15 18l-6-6 6-6" />
          </svg>
        </button>
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate font-serif text-[15px] font-semibold text-kinora-text">{book.title}</span>
          <span className="flex-shrink-0 text-[11px] text-kinora-muted">· {book.author}</span>
        </div>
        <div className="flex-1" />
        {session.live && (
          <div aria-live="polite" className="flex items-center gap-2 text-[10px] font-medium text-kinora-muted">
            <span
              className="inline-flex h-1.5 w-1.5 rounded-full"
              style={{ background: session.bufferAhead ? "#8aa887" : "rgba(232,226,216,0.35)" }}
            />
            <span>{status}</span>
          </div>
        )}
      </div>

      <div className="relative flex min-h-0 flex-1 flex-col">
        <ScrollFilmEngine
          key={book.id}
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

        <div className="pointer-events-none absolute bottom-6 right-3 top-6 w-1 rounded-full" aria-hidden style={{ background: "rgba(255,255,255,0.06)" }}>
          <div
            className="absolute inset-x-0 top-0 rounded-full"
            style={{ height: `${progress * 100}%`, background: "rgba(232,226,216,0.35)", transition: reduce ? "none" : `height ${RAIL_SETTLE}` }}
          />
          {session.live && (
            <div
              className="absolute inset-x-0 rounded-full"
              style={{
                top: `${progress * 100}%`,
                height: `${Math.min(0.18, Math.max(0, (session.bufferAhead ?? 0) / 30)) * 100}%`,
                background: "rgba(232,226,216,0.15)",
                transition: reduce ? "none" : `top ${RAIL_SETTLE}, height 0.4s ease`,
              }}
            />
          )}
          {session.live &&
            session.shots.map((shot) =>
              shot.source_span ? (
                <div
                  key={shot.shot_id}
                  className="absolute left-1/2 h-[2px] w-[7px] -translate-x-1/2 rounded-full"
                  style={{ top: `${(shot.source_span.word_range[0] / totalWords) * 100}%`, background: "rgba(255,255,255,0.22)" }}
                />
              ) : null,
            )}
          <div
            className="absolute left-1/2 h-2 w-2 -translate-x-1/2 rounded-full"
            style={{ top: `calc(${progress * 100}% - 4px)`, background: "rgba(232,226,216,0.8)", transition: reduce ? "none" : `top ${RAIL_SETTLE}` }}
          />
        </div>

        <AnimatePresence>
          {showWarmUp && <WarmUp key="warmup" state={state} session={session} bookTitle={book.title} reduce={reduce} />}
        </AnimatePresence>
      </div>
    </div>
  );
}
