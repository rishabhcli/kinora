// BookPreviewCard — a discovery card with a rich hover preview. Hovering for the
// intent delay (preview.ts) expands a glass panel with the book's metadata,
// "why we picked it" reason, progress, and quick actions (Play / More like this /
// Not interested). Keyboard: focus + Enter opens; the panel is also reachable.
//
// The hover-intent timing is driven by the pure `reducePreview` reducer; this
// component just owns the single timer the reducer asks it to arm.
import { useCallback, useEffect, useReducer, useRef } from "react";
import type { DiscoveryBook } from "../../lib/discovery/types";
import {
  reducePreview,
  initialPreviewState,
  isPreviewOpen,
  DEFAULT_PREVIEW_CONFIG,
  type PreviewEvent,
  type PreviewConfig,
} from "../../lib/discovery/preview";
import { BookCoverImage } from "../SkeletonShimmer";
import { useReducedMotionPref } from "../../a11y/useReducedMotionPref";
import { ensureDiscoveryStyles } from "./styleInjection";

export interface PreviewActions {
  onOpen?: (book: DiscoveryBook) => void;
  onMoreLikeThis?: (book: DiscoveryBook) => void;
  onNotInterested?: (book: DiscoveryBook) => void;
  /** Fired when the rich preview first opens (for a "preview" signal). */
  onPreview?: (book: DiscoveryBook) => void;
}

interface BookPreviewCardProps extends PreviewActions {
  book: DiscoveryBook;
  /** Optional "Because you read …" reason shown in the preview. */
  reason?: string;
  config?: PreviewConfig;
  /** Roving-tabindex: 0 when this is the active cell, -1 otherwise. */
  tabIndex?: number;
  /** Stable id for the focusable control (roving-grid focus target). */
  controlId?: string;
  /** Notify the parent grid that this cell received focus (roving sync). */
  onCellFocus?: () => void;
}

function PlayIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}

export default function BookPreviewCard({
  book,
  reason,
  config = DEFAULT_PREVIEW_CONFIG,
  tabIndex = 0,
  controlId,
  onCellFocus,
  onOpen,
  onMoreLikeThis,
  onNotInterested,
  onPreview,
}: BookPreviewCardProps) {
  const reduce = useReducedMotionPref();
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wasOpen = useRef(false);

  // Wrap the pure reducer so it can arm the host timer as a side effect.
  const [state, rawDispatch] = useReducer(
    (s: ReturnType<typeof initialPreviewState>, ev: PreviewEvent) => {
      const { state: next, armInMs } = reducePreview(s, ev, Date.now(), config);
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (armInMs !== null) {
        timerRef.current = setTimeout(() => dispatchRef.current({ type: "tick" }), armInMs);
      }
      return next;
    },
    undefined,
    initialPreviewState,
  );

  // Stable dispatch reference for the timer callback (avoids stale closure).
  const dispatchRef = useRef(rawDispatch);
  dispatchRef.current = rawDispatch;
  const dispatch = useCallback((ev: PreviewEvent) => dispatchRef.current(ev), []);

  useEffect(() => {
    ensureDiscoveryStyles();
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, []);

  const open = isPreviewOpen(state, book.id);

  // Fire the onPreview signal exactly once per open transition.
  useEffect(() => {
    if (open && !wasOpen.current) {
      wasOpen.current = true;
      onPreview?.(book);
    } else if (!open) {
      wasOpen.current = false;
    }
  }, [open, book, onPreview]);

  const handleOpen = () => {
    dispatch({ type: "dismiss" });
    onOpen?.(book);
  };

  return (
    <div
      className="relative flex-shrink-0 w-[150px]"
      style={{ perspective: 600 }}
      onMouseEnter={() => dispatch({ type: "enter", id: book.id })}
      onMouseLeave={() => dispatch({ type: "leave", id: book.id })}
      data-testid={`preview-card-${book.id}`}
    >
      <div
        id={controlId}
        role="button"
        tabIndex={tabIndex}
        aria-label={`${book.title} by ${book.author}${book.genre ? `, ${book.genre}` : ""}${book.progress > 0 ? `, ${book.progress}% read` : ""}`}
        aria-expanded={open}
        className="group cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-amber-400/70 rounded-md"
        onClick={handleOpen}
        onFocus={() => { onCellFocus?.(); dispatch({ type: "enter", id: book.id }); }}
        onBlur={() => dispatch({ type: "leave", id: book.id })}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            handleOpen();
          } else if (e.key === "Escape") {
            dispatch({ type: "dismiss" });
          }
        }}
      >
        <div className="book-cover w-[150px] relative" style={{ background: book.coverGradient }}>
          <div className="book-cover-inner">
            <BookCoverImage
              src={book.coverImage}
              alt={book.title}
              className="absolute inset-0 w-full h-full object-cover"
              fallbackBackground={book.coverGradient}
            />
            <div className="absolute inset-0 book-spine" />
            <div className="absolute inset-0 book-gloss" />
            {book.progress > 0 && book.progress < 100 && (
              <div className="absolute bottom-0 left-0 right-0 h-[3px] bg-black/40">
                <div
                  className="h-full"
                  style={{ width: `${book.progress}%`, background: "rgba(212,164,78,0.95)" }}
                />
              </div>
            )}
            {book.live && (
              <div
                className="absolute bottom-1 left-1 flex items-center gap-1 rounded-full px-1.5 py-0.5"
                style={{ background: "rgba(0,0,0,0.55)" }}
              >
                <span className="inline-flex h-1.5 w-1.5 rounded-full" style={{ background: "#34d399", boxShadow: "0 0 5px #34d399" }} />
                <span className="text-[7px] font-bold tracking-wider text-white/90">LIVE</span>
              </div>
            )}
          </div>
        </div>
        <h3 className="text-[11px] font-medium text-kinora-text truncate leading-tight mt-1.5">{book.title}</h3>
        <p className="text-[10px] text-kinora-muted truncate">{book.author}</p>
      </div>

      {/* Rich hover preview panel */}
      {open && (
        <div
          role="dialog"
          aria-label={`${book.title} preview`}
          className="absolute z-30 left-1/2 top-0"
          style={{
            transform: "translateY(-8%)",
            width: 240,
            marginLeft: -120,
            borderRadius: 12,
            background: "rgb(var(--k-surface-raised-rgb, 26 23 20) / 0.98)",
            border: "1px solid rgba(255,255,255,0.09)",
            boxShadow: "0 18px 48px -12px rgba(0,0,0,0.7)",
            overflow: "hidden",
            animation: reduce ? undefined : "discovery-pop 140ms cubic-bezier(0.22,1,0.36,1)",
          }}
          onMouseEnter={() => dispatch({ type: "enterPreview" })}
          onMouseLeave={() => dispatch({ type: "leavePreview" })}
        >
          <div className="relative" style={{ height: 120, background: book.coverGradient }}>
            <BookCoverImage
              src={book.coverImage}
              alt=""
              className="absolute inset-0 w-full h-full object-cover"
              fallbackBackground={book.coverGradient}
            />
            <div className="absolute inset-0" style={{ background: "linear-gradient(180deg, transparent 30%, rgba(0,0,0,0.75) 100%)" }} />
          </div>
          <div className="p-3">
            <h4 className="text-[13px] font-semibold text-kinora-text leading-tight">{book.title}</h4>
            <p className="text-[10px] text-kinora-muted mb-2">{book.author}</p>
            {reason && (
              <p className="text-[10px] mb-2" style={{ color: "rgba(212,164,78,0.92)" }}>
                {reason}
              </p>
            )}
            <div className="flex items-center gap-2 mb-2.5 flex-wrap">
              {book.genre && (
                <span className="rounded px-1.5 py-px text-[8px] font-semibold tracking-wide uppercase" style={{ background: "rgba(212,164,78,0.15)", color: "rgba(212,164,78,0.92)" }}>
                  {book.genre}
                </span>
              )}
              {book.era && <span className="text-[9px] text-kinora-muted">{book.era}</span>}
              {book.progress > 0 && book.progress < 100 && (
                <span className="text-[9px] text-kinora-muted">{book.progress}% read</span>
              )}
            </div>
            <div className="flex items-center gap-1.5">
              <button
                onClick={handleOpen}
                className="flex items-center gap-1 rounded-full px-3 py-1 text-[10px] font-semibold text-amber-950"
                style={{ background: "rgba(212,164,78,0.95)" }}
              >
                <PlayIcon />
                {book.progress > 0 ? "Resume" : "Play"}
              </button>
              {onMoreLikeThis && (
                <button
                  onClick={() => onMoreLikeThis(book)}
                  className="rounded-full px-2.5 py-1 text-[10px] text-kinora-muted hover:text-kinora-text hover:bg-white/[0.06] transition-colors"
                >
                  More like this
                </button>
              )}
              {onNotInterested && (
                <button
                  aria-label="Not interested"
                  title="Not interested"
                  onClick={() => {
                    dispatch({ type: "dismiss" });
                    onNotInterested(book);
                  }}
                  className="ml-auto rounded-full w-6 h-6 flex items-center justify-center text-kinora-muted hover:text-red-300 hover:bg-white/[0.06] transition-colors"
                >
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
                    <path d="M18 6 6 18M6 6l12 12" />
                  </svg>
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
