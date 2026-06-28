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

// Persisted per-book bookmark + highlight state. Local-only (no backend) so the
// feature works offline and across the fallback path; can be promoted to the
// server when a notes API exists.
const BM_KEY = (id: string) => `kinora.bookmark.${id}`;
const HL_KEY = (id: string) => `kinora.highlights.${id}`;

interface BookmarkData {
  on: boolean;
  scrollFraction: number;
}

function readBookmark(id: string): BookmarkData {
  try {
    const raw = localStorage.getItem(BM_KEY(id));
    if (!raw) return { on: false, scrollFraction: 0 };
    const parsed = JSON.parse(raw) as Partial<BookmarkData>;
    return {
      on: parsed.on ?? true,
      scrollFraction: parsed.scrollFraction ?? 0,
    };
  } catch { return { on: false, scrollFraction: 0 }; }
}
function writeBookmark(id: string, data: BookmarkData): void {
  try { localStorage.setItem(BM_KEY(id), JSON.stringify(data)); } catch { /* storage blocked */ }
}
function readHighlightCount(id: string): number {
  try {
    const raw = localStorage.getItem(HL_KEY(id));
    if (!raw) return 0;
    const parsed = JSON.parse(raw) as unknown;
    return Array.isArray(parsed) ? parsed.length : 0;
  } catch { return 0; }
}

export function ReadingRoomShell({
  book,
  onClose,
  state,
  dispatch,
  session,
  reduce,
  generateVideo,
  onToggleGenerate,
}: {
  book: Book;
  onClose: () => void;
  state: MachineState;
  dispatch: Dispatch<MachineEvent>;
  session: FilmSession;
  reduce: boolean;
  generateVideo: boolean;
  onToggleGenerate: (next: boolean) => void;
}) {
  const { t } = useTranslation();
  const rootRef = useRef<HTMLDivElement>(null);
  const { prefs, update } = useReadingPrefs();
  const [progress, setProgress] = useState(0);
  const [controlsOpen, setControlsOpen] = useState(false);
  const [bookmarked, setBookmarked] = useState(() => readBookmark(book.id).on);
  const [highlightMode, setHighlightMode] = useState(false);
  const [highlightCount, setHighlightCount] = useState(() => readHighlightCount(book.id));

  // Sync per-book state when switching books and restore scroll position if bookmarked.
  useEffect(() => {
    const bm = readBookmark(book.id);
    setBookmarked(bm.on);
    setHighlightCount(readHighlightCount(book.id));
    setHighlightMode(false);
    if (bm.on && bm.scrollFraction > 0) {
      const t = window.setTimeout(() => {
        const scrollEl = rootRef.current?.querySelector<HTMLElement>("[data-reading-scroll]");
        if (scrollEl) {
          scrollEl.scrollTop = bm.scrollFraction * (scrollEl.scrollHeight - scrollEl.clientHeight);
        }
      }, 120);
      return () => window.clearTimeout(t);
    }
  }, [book.id]);

  const toggleBookmark = useCallback(() => {
    setBookmarked((prev) => {
      const next = !prev;
      const scrollEl = rootRef.current?.querySelector<HTMLElement>("[data-reading-scroll]");
      const scrollFraction = scrollEl
        ? scrollEl.scrollTop / Math.max(1, scrollEl.scrollHeight - scrollEl.clientHeight)
        : 0;
      writeBookmark(book.id, { on: next, scrollFraction });
      return next;
    });
  }, [book.id]);

  // When highlight mode is ON, saving the current text selection as a highlight
  // is a one-keystroke action ("h" or click the Save button). Selections are
  // captured by text content + a coarse timestamp — no exact ranges yet, but
  // enough to drive a notes panel later.
  const saveSelection = useCallback(() => {
    const sel = window.getSelection?.();
    const text = sel?.toString().trim();
    if (!text) return;
    try {
      const raw = localStorage.getItem(HL_KEY(book.id));
      const arr: Array<{ text: string; at: number }> = raw ? (JSON.parse(raw) as Array<{ text: string; at: number }>) : [];
      arr.push({ text, at: Date.now() });
      localStorage.setItem(HL_KEY(book.id), JSON.stringify(arr));
      setHighlightCount(arr.length);
      sel?.removeAllRanges();
    } catch { /* storage blocked */ }
  }, [book.id]);

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
          <span className="font-serif text-[15px] font-semibold truncate text-kinora-text">
            {book.title}
          </span>
          <span className="text-[11px] text-kinora-muted flex-shrink-0">· {book.author}</span>
        </div>
        <div className="flex-1" />

        {/* AI film generation toggle — explicit user opt-in (default OFF in ReadingRoom) */}
        <button
          onClick={() => onToggleGenerate(!generateVideo)}
          aria-pressed={generateVideo}
          aria-label={generateVideo ? "Disable AI film generation" : "Enable AI film generation"}
          title={generateVideo ? "AI film: ON — click to disable" : "AI film: OFF — click to enable"}
          className="flex items-center gap-2 px-2.5 py-1.5 text-[11px] font-medium transition-colors"
          style={{
            borderRadius: 6,
            background: generateVideo ? "rgba(212,164,78,0.12)" : "rgba(255,255,255,0.04)",
            border: `1px solid ${generateVideo ? "rgba(212,164,78,0.25)" : "rgba(255,255,255,0.08)"}`,
            color: generateVideo ? "#e8c878" : "rgba(232,226,216,0.7)",
          }}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
            <rect x="2" y="6" width="14" height="12" rx="2" />
            <path d="M22 8l-6 4 6 4z" />
          </svg>
          <span>AI Film</span>
          <span
            aria-hidden
            className="inline-block"
            style={{
              width: 18,
              height: 10,
              borderRadius: 5,
              background: generateVideo ? "#d4a44e" : "rgba(255,255,255,0.12)",
              position: "relative",
              transition: "background 0.15s",
            }}
          >
            <span
              style={{
                position: "absolute",
                top: 1,
                left: generateVideo ? 9 : 1,
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: "#0a0908",
                transition: "left 0.15s",
              }}
            />
          </span>
        </button>

        {/* Bookmark toggle */}
        <button
          onClick={toggleBookmark}
          aria-pressed={bookmarked}
          aria-label={bookmarked ? "Remove bookmark" : "Bookmark this book"}
          title={bookmarked ? "Bookmarked" : "Add bookmark"}
          className="flex items-center justify-center transition-colors"
          style={{
            width: 30,
            height: 30,
            borderRadius: 6,
            background: bookmarked ? "rgba(212,164,78,0.12)" : "rgba(255,255,255,0.04)",
            border: `1px solid ${bookmarked ? "rgba(212,164,78,0.25)" : "rgba(255,255,255,0.08)"}`,
            color: bookmarked ? "#e8c878" : "rgba(232,226,216,0.7)",
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill={bookmarked ? "currentColor" : "none"} stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
            <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" />
          </svg>
        </button>

        {/* Highlight mode toggle */}
        <button
          onClick={() => setHighlightMode((v) => !v)}
          aria-pressed={highlightMode}
          aria-label={highlightMode ? "Exit highlight mode" : "Enter highlight mode"}
          title={highlightMode ? "Highlight mode: ON — select text and click Save" : "Highlight mode: OFF"}
          className="flex items-center gap-1.5 px-2 py-1.5 text-[11px] font-medium transition-colors"
          style={{
            borderRadius: 6,
            background: highlightMode ? "rgba(212,164,78,0.12)" : "rgba(255,255,255,0.04)",
            border: `1px solid ${highlightMode ? "rgba(212,164,78,0.25)" : "rgba(255,255,255,0.08)"}`,
            color: highlightMode ? "#e8c878" : "rgba(232,226,216,0.7)",
          }}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
            <path d="M15 4l5 5-9 9H6v-5z" />
            <path d="M14 5l5 5" />
          </svg>
          <span>{highlightCount > 0 ? `Highlights · ${highlightCount}` : "Highlight"}</span>
        </button>

        {highlightMode && (
          <button
            onClick={saveSelection}
            aria-label="Save current selection as a highlight"
            title="Save selection"
            className="px-2.5 py-1.5 text-[11px] font-semibold transition-colors"
            style={{
              borderRadius: 6,
              background: "linear-gradient(180deg, #e8c878 0%, #d4a44e 100%)",
              border: "none",
              color: "#0a0908",
            }}
          >
            Save
          </button>
        )}

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
            className="flex items-center gap-2 rounded-md px-2.5 py-1 text-[10px] font-medium"
            style={{
              background: "rgba(255,255,255,0.04)",
              border: "1px solid rgba(255,255,255,0.08)",
              color: "rgba(232,226,216,0.85)",
            }}
          >
            <span
              className="inline-flex h-1.5 w-1.5 rounded-full"
              style={{ background: "rgba(232,226,216,0.55)" }}
            />
            <span>{pill}</span>
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
            style={{ top: `calc(${progress * 100}% - 4px)`, background: "rgba(232,226,216,0.8)", transition: reduce ? "none" : `top ${RAIL_SETTLE}` }}
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
