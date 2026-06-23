import {
  clampZoom,
  fitZoom,
  focusWordAtReadingLine,
  prefetchRange,
  queryKeys,
  type RenderedPage,
  visiblePageAtLine,
} from "@kinora/core";
import { useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { type ReadingSettings, type ReadingTheme } from "../../lib/readingTheme";
import { getReadingPosition, setReadingPosition, useReaderView } from "../../lib/readerView";
import { PdfPageRow } from "./PdfPageRow";
import { DEFAULT_PAGE_RATIO, pageQueryOptions, type WordBox } from "./pdfPage";

interface PdfReadingColumnProps {
  bookId: string;
  numPages: number | null;
  /** The page the playhead wants visible (followed only while `autoFollow`). */
  page: number;
  /** True while video owns the playhead — follow the karaoke word automatically. */
  autoFollow: boolean;
  /** Global word_index to paint as the karaoke highlight, or null. */
  highlightWordIndex: number | null;
  settings: ReadingSettings;
  theme: ReadingTheme;
  /** A word was clicked — seek the shared playhead there. */
  onSeekWord: (word: number) => void;
  /** The reader scrolled — report the reading-line focus word `w` (§4.3). */
  onReportScroll: (word: number) => void;
  /** The top-of-view page changed (drives the footer + bookmark). */
  onVisiblePageChange: (page: number) => void;
}

/** Per-page geometry + words, registered live by the mounted {@link PdfPageRow}s. */
interface PageEntry {
  el: HTMLElement;
  words: WordBox[];
}

/** ms a real scroll gesture keeps "reading intent" alive (so we report focus
 *  for genuine reader scrolls but not for our own video-follow scrolling). */
const READER_INTENT_MS = 900;
const HPAD = 40; // the row's horizontal padding (px-5), excluded from the page box
const ZOOM_STEP = 1.15;
const PREFETCH_RADIUS = 4;

/**
 * The left pane (kinora.md §5.2): **real PDF pages, virtualised** so only the
 * visible pages live in the DOM — layout-faithful typography, illustrations and
 * manga panels intact, scrolling smoothly across hundreds of pages.
 *
 * It is the client half of the bidirectional sync (scroll→video spy at the
 * reading line, §4.3; video→scroll follow of the karaoke word) and a real
 * reader: fit-width / fit-page / zoom with pan, keyboard navigation, a
 * go-to-page jump, neighbour prefetch for smooth fast-scroll, and a persisted
 * reading position so the book reopens where it was left.
 */
export function PdfReadingColumn({
  bookId,
  numPages,
  page,
  autoFollow,
  highlightWordIndex,
  settings,
  theme,
  onSeekWord,
  onReportScroll,
  onVisiblePageChange,
}: PdfReadingColumnProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pagesRef = useRef(new Map<number, PageEntry>());
  const rafRef = useRef<number | null>(null);
  const readerIntentUntil = useRef(0);
  const lastFocusRef = useRef<number | null>(null);
  const visiblePageRef = useRef(1);
  const restoredRef = useRef(false);
  const panRef = useRef({ active: false, moved: false, x: 0, y: 0, left: 0, top: 0 });
  const suppressClickRef = useRef(false);
  const pendingCenterRef = useRef<{ fracX: number } | null>(null);

  const [pane, setPane] = useState({ width: 0, height: 0 });
  const [learnedRatio, setLearnedRatio] = useState<number | null>(null);
  const [visiblePage, setVisiblePage] = useState(1);
  const [gotoText, setGotoText] = useState("");
  const [gotoFocused, setGotoFocused] = useState(false);

  const { view, setFitMode, setZoom } = useReaderView();
  const queryClient = useQueryClient();

  const count = Math.max(0, numPages ?? 0);

  // The rendered page width: fit-to-pane (capped) scaled by the effective zoom
  // for the current fit mode. Pages zoomed past the pane scroll horizontally so
  // the reader can pan a manga spread or magnify fine print.
  const baseFit = Math.min((pane.width || 720) - HPAD, 736);
  const ratioForFit = learnedRatio ?? DEFAULT_PAGE_RATIO;
  const effectiveZoom = fitZoom(view.fitMode, pane, ratioForFit, baseFit, view.zoom);
  const surfaceWidth = Math.max(160, Math.round(baseFit * effectiveZoom));
  const zoomedIn = effectiveZoom > 1.01;

  const trackWidth = Math.max(pane.width, surfaceWidth + HPAD);

  // A row reserves space from the book's learned page shape until its image loads.
  const estimateSize = useCallback(() => {
    return Math.round(surfaceWidth / (learnedRatio ?? DEFAULT_PAGE_RATIO) + 32); // + py-4
  }, [surfaceWidth, learnedRatio]);

  const virtualizer = useVirtualizer({
    count,
    getScrollElement: () => scrollRef.current,
    estimateSize,
    overscan: 3,
  });

  // Rows register their rendered surface + words here for the scroll-spy.
  const registerPage = useCallback((p: number, el: HTMLElement | null, words: WordBox[]) => {
    if (el) pagesRef.current.set(p, { el, words });
    else pagesRef.current.delete(p);
  }, []);

  const handleAspect = useCallback((ratio: number) => {
    setLearnedRatio((prev) => prev ?? ratio);
  }, []);

  // Stable seek handler so the memoised page rows aren't invalidated each render.
  const seekRef = useRef(onSeekWord);
  seekRef.current = onSeekWord;
  const handleSeek = useCallback((word: number) => seekRef.current(word), []);

  // Latest zoom action for the (once-attached) wheel listener to read.
  const zoomActionRef = useRef<(factor: number) => void>(() => {});
  zoomActionRef.current = (factor: number) => setZoom(clampZoom(effectiveZoom * factor));

  const goToPage = useCallback(
    (p: number) => {
      if (count === 0) return;
      const idx = Math.min(Math.max(0, p - 1), count - 1);
      readerIntentUntil.current = performance.now() + 1200; // reader-initiated jump
      virtualizer.scrollToIndex(idx, { align: "start", behavior: "smooth" });
    },
    [count, virtualizer],
  );

  // --- the scroll-spy: focus word + visible page at the reading line -------- #
  const runSpy = useCallback(() => {
    rafRef.current = null;
    const sc = scrollRef.current;
    if (!sc) return;
    const rect = sc.getBoundingClientRect();
    const lineY = rect.height / 3; // the reading line (§4.3), in viewport coords
    const pages: RenderedPage[] = [];
    for (const [p, entry] of pagesRef.current) {
      const r = entry.el.getBoundingClientRect();
      pages.push({ page: p, top: r.top - rect.top, height: r.height, words: entry.words });
    }
    const visible = visiblePageAtLine(pages, lineY) ?? visiblePageRef.current;
    if (visible !== visiblePageRef.current) {
      visiblePageRef.current = visible;
      setVisiblePage(visible);
      onVisiblePageChange(visible);
      setReadingPosition(bookId, visible);
    }
    // Only a genuine reader scroll drives the playhead — our own video-follow
    // scrolls leave reader-intent untouched, so they never grab ownership.
    if (performance.now() >= readerIntentUntil.current) return;
    const focus = focusWordAtReadingLine(pages, lineY);
    if (focus !== null && focus !== lastFocusRef.current) {
      lastFocusRef.current = focus;
      onReportScroll(focus);
    }
  }, [onReportScroll, onVisiblePageChange, bookId]);

  const scheduleSpy = useCallback(() => {
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(runSpy);
  }, [runSpy]);

  // ⌘/Ctrl-wheel zooms (non-passive so it can preventDefault); any wheel also
  // marks reading intent so a genuine scroll reports focus.
  useEffect(() => {
    const sc = scrollRef.current;
    if (!sc) return;
    const onWheel = (event: WheelEvent) => {
      readerIntentUntil.current = performance.now() + READER_INTENT_MS;
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        zoomActionRef.current(event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP);
      }
    };
    sc.addEventListener("wheel", onWheel, { passive: false });
    return () => sc.removeEventListener("wheel", onWheel);
  }, []);

  // Track the pane size so row-height estimates + fit-page stay accurate.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    setPane({ width: el.clientWidth, height: el.clientHeight });
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setPane({ width: entry.contentRect.width, height: entry.contentRect.height });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Re-run the spy when zoom/size reshapes the layout, and apply a pending
  // double-click recentre once the new width has taken effect.
  useEffect(() => {
    scheduleSpy();
    const pending = pendingCenterRef.current;
    const sc = scrollRef.current;
    if (pending && sc) {
      pendingCenterRef.current = null;
      sc.scrollLeft = Math.max(0, pending.fracX * sc.scrollWidth - sc.clientWidth / 2);
    }
  }, [surfaceWidth, scheduleSpy]);

  // Restore the saved reading position once the page count is known (unless video
  // already owns the playhead). A restore must not report focus as a scroll.
  useEffect(() => {
    if (restoredRef.current || count === 0) return;
    restoredRef.current = true;
    if (autoFollow) return;
    const saved = getReadingPosition(bookId);
    if (saved && saved > 1) {
      readerIntentUntil.current = 0;
      virtualizer.scrollToIndex(Math.min(saved - 1, count - 1), { align: "start" });
    }
  }, [count, autoFollow, bookId, virtualizer]);

  // Prefetch (and warm-decode) the neighbouring pages so fast scrolling never
  // lands on a blank page (§5.2 "a few seconds ahead").
  useEffect(() => {
    if (count === 0) return;
    for (const p of prefetchRange(visiblePage, count, PREFETCH_RADIUS)) {
      void queryClient.prefetchQuery(pageQueryOptions(bookId, p)).then(() => {
        const cached = queryClient.getQueryData(queryKeys.page(bookId, p)) as
          | { image_url?: string | null }
          | undefined;
        if (cached?.image_url) {
          const img = new Image();
          img.decoding = "async";
          img.src = cached.image_url;
        }
      });
    }
  }, [visiblePage, count, bookId, queryClient]);

  // --- video → scroll: keep the spoken word comfortably in view ----------- #
  useEffect(() => {
    if (!autoFollow) return;
    const sc = scrollRef.current;
    if (!sc) return;
    const rect = sc.getBoundingClientRect();

    let wordY: number | null = null;
    if (highlightWordIndex !== null) {
      for (const entry of pagesRef.current.values()) {
        const w = entry.words.find((x) => x.word_index === highlightWordIndex);
        if (!w) continue;
        const r = entry.el.getBoundingClientRect();
        wordY = r.top - rect.top + (w.bbox[1] + w.bbox[3] / 2) * r.height;
        break;
      }
    }

    if (wordY !== null) {
      if (wordY >= rect.height * 0.18 && wordY <= rect.height * 0.62) return;
      sc.scrollTo({ top: Math.max(0, sc.scrollTop + wordY - rect.height / 3), behavior: "smooth" });
      return;
    }
    if (count > 0) {
      virtualizer.scrollToIndex(Math.min(Math.max(0, page - 1), count - 1), { align: "start" });
    }
  }, [autoFollow, page, highlightWordIndex, count, virtualizer]);

  // --- keyboard navigation (the pane is focusable) ------------------------- #
  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    readerIntentUntil.current = performance.now() + READER_INTENT_MS;
    switch (event.key) {
      case "ArrowRight":
      case "PageDown":
        event.preventDefault();
        goToPage(visiblePage + 1);
        break;
      case "ArrowLeft":
      case "PageUp":
        event.preventDefault();
        goToPage(visiblePage - 1);
        break;
      case "Home":
        event.preventDefault();
        goToPage(1);
        break;
      case "End":
        event.preventDefault();
        goToPage(count);
        break;
      case "+":
      case "=":
        event.preventDefault();
        setZoom(clampZoom(effectiveZoom * ZOOM_STEP));
        break;
      case "-":
        event.preventDefault();
        setZoom(clampZoom(effectiveZoom / ZOOM_STEP));
        break;
      case "0":
        event.preventDefault();
        setFitMode("width");
        break;
      // ArrowUp / ArrowDown / Space fall through to native scrolling.
      default:
        break;
    }
  };

  // --- drag-to-pan when zoomed + double-click smart zoom ------------------- #
  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    readerIntentUntil.current = performance.now() + READER_INTENT_MS;
    if (!zoomedIn) return;
    const sc = scrollRef.current;
    if (!sc) return;
    panRef.current = {
      active: true,
      moved: false,
      x: event.clientX,
      y: event.clientY,
      left: sc.scrollLeft,
      top: sc.scrollTop,
    };
  };
  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const pan = panRef.current;
    if (!pan.active) return;
    const sc = scrollRef.current;
    if (!sc) return;
    const dx = event.clientX - pan.x;
    const dy = event.clientY - pan.y;
    if (!pan.moved && Math.abs(dx) + Math.abs(dy) > 4) {
      pan.moved = true;
      sc.setPointerCapture?.(event.pointerId);
    }
    if (pan.moved) {
      sc.scrollLeft = pan.left - dx;
      sc.scrollTop = pan.top - dy;
    }
  };
  const onPointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const pan = panRef.current;
    if (pan.active && pan.moved) suppressClickRef.current = true;
    pan.active = false;
    scrollRef.current?.releasePointerCapture?.(event.pointerId);
  };
  const onClickCapture = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      event.stopPropagation();
      event.preventDefault();
    }
  };
  const onDoubleClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (zoomedIn) {
      setFitMode("width");
      return;
    }
    const sc = scrollRef.current;
    if (sc) {
      const localX = sc.scrollLeft + (event.clientX - sc.getBoundingClientRect().left);
      pendingCenterRef.current = { fracX: localX / Math.max(1, sc.scrollWidth) };
    }
    setZoom(2);
  };

  const commitGoto = () => {
    const n = Number.parseInt(gotoText, 10);
    if (Number.isFinite(n)) goToPage(Math.min(Math.max(1, n), count || 1));
    setGotoFocused(false);
  };

  const scopeStyle = {
    ...(theme.vars as Record<string, string>),
    filter: settings.brightness < 1 ? `brightness(${settings.brightness})` : undefined,
  } as CSSProperties;

  const total = numPages ?? visiblePage;
  const items = virtualizer.getVirtualItems();

  // Route the karaoke highlight to the single mounted page that owns the word, so
  // only that row re-renders as playback advances (the rest stay memoised).
  let highlightPage: number | null = null;
  if (highlightWordIndex !== null) {
    for (const [p, entry] of pagesRef.current) {
      if (entry.words.some((w) => w.word_index === highlightWordIndex)) {
        highlightPage = p;
        break;
      }
    }
  }

  const fitButton = (mode: "width" | "page", label: string) => (
    <button
      type="button"
      aria-pressed={view.fitMode === mode}
      onClick={() => setFitMode(mode)}
      className={`rounded-full px-2.5 py-1 font-sans text-[11px] uppercase tracking-[0.12em] transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--page-accent)] ${
        view.fitMode === mode ? "opacity-100" : "opacity-55 hover:opacity-85"
      }`}
      style={view.fitMode === mode ? { color: "var(--page-accent)" } : undefined}
    >
      {label}
    </button>
  );

  return (
    <div
      data-reading-theme={theme.id}
      className="reading-scope flex h-full min-h-0 flex-col"
      style={scopeStyle}
    >
      <div
        ref={scrollRef}
        onScroll={scheduleSpy}
        onKeyDown={onKeyDown}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onClickCapture={onClickCapture}
        onDoubleClick={onDoubleClick}
        tabIndex={0}
        role="region"
        aria-label="Book pages"
        className="min-h-0 flex-1 overflow-auto focus:outline-none"
        style={{ cursor: zoomedIn ? "grab" : undefined, userSelect: zoomedIn ? "none" : "auto" }}
      >
        {count === 0 ? (
          <div className="flex h-full items-center justify-center px-8">
            <div
              aria-hidden
              className="shimmer w-full max-w-[46rem] rounded-[10px]"
              style={{ aspectRatio: String(DEFAULT_PAGE_RATIO) }}
            />
          </div>
        ) : (
          <div style={{ height: virtualizer.getTotalSize(), position: "relative", width: trackWidth }}>
            {items.map((item) => (
              <div
                key={item.key}
                data-index={item.index}
                ref={virtualizer.measureElement}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: `translateY(${item.start}px)`,
                }}
              >
                <PdfPageRow
                  bookId={bookId}
                  pageNumber={item.index + 1}
                  highlightWordIndex={item.index + 1 === highlightPage ? highlightWordIndex : null}
                  settings={settings}
                  estimatedRatio={learnedRatio}
                  surfaceWidth={surfaceWidth}
                  onSeekWord={handleSeek}
                  registerPage={registerPage}
                  onAspect={handleAspect}
                />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Fit + zoom · a go-to-page jump · page steppers. */}
      <footer
        className="flex shrink-0 items-center justify-between gap-2 px-5 py-2.5 md:px-8"
        style={{ color: "var(--page-ink-soft)" }}
      >
        <div className="flex items-center gap-0.5">
          {fitButton("width", "Width")}
          {fitButton("page", "Page")}
          <span className="mx-1 h-4 w-px" style={{ background: "var(--page-rule)" }} />
          <button
            type="button"
            aria-label="Zoom out"
            onClick={() => setZoom(clampZoom(effectiveZoom / ZOOM_STEP))}
            className="flex h-7 w-7 items-center justify-center rounded-full text-[15px] leading-none opacity-80 transition hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--page-accent)]"
          >
            −
          </button>
          <button
            type="button"
            aria-label="Reset zoom to fit width"
            onClick={() => setFitMode("width")}
            className="min-w-[3rem] rounded-full px-1 font-sans text-[11px] tabular-nums opacity-70 transition hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--page-accent)]"
          >
            {Math.round(effectiveZoom * 100)}%
          </button>
          <button
            type="button"
            aria-label="Zoom in"
            onClick={() => setZoom(clampZoom(effectiveZoom * ZOOM_STEP))}
            className="flex h-7 w-7 items-center justify-center rounded-full text-[15px] leading-none opacity-80 transition hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--page-accent)]"
          >
            +
          </button>
        </div>

        <div className="flex items-center gap-1.5 font-sans text-[11px] uppercase tracking-[0.14em] opacity-70">
          <span>Page</span>
          <input
            type="text"
            inputMode="numeric"
            aria-label="Go to page"
            value={gotoFocused ? gotoText : String(visiblePage)}
            onFocus={(e) => {
              setGotoFocused(true);
              setGotoText(String(visiblePage));
              e.currentTarget.select();
            }}
            onChange={(e) => setGotoText(e.target.value.replace(/[^0-9]/g, ""))}
            onBlur={commitGoto}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === "Enter") {
                e.preventDefault();
                commitGoto();
                e.currentTarget.blur();
              } else if (e.key === "Escape") {
                setGotoFocused(false);
                e.currentTarget.blur();
              }
            }}
            className="w-10 rounded-md border border-[color:var(--page-rule)] bg-transparent px-1 py-0.5 text-center tabular-nums tracking-normal focus:outline-none focus:ring-2 focus:ring-[color:var(--page-accent)]"
          />
          <span>of {total}</span>
        </div>

        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label="Previous page"
            disabled={visiblePage <= 1}
            onClick={() => goToPage(visiblePage - 1)}
            className="flex h-7 w-7 items-center justify-center rounded-full opacity-80 transition enabled:hover:opacity-100 disabled:opacity-25 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--page-accent)]"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <path d="m15 18-6-6 6-6" />
            </svg>
          </button>
          <button
            type="button"
            aria-label="Next page"
            disabled={visiblePage >= total}
            onClick={() => goToPage(visiblePage + 1)}
            className="flex h-7 w-7 items-center justify-center rounded-full opacity-80 transition enabled:hover:opacity-100 disabled:opacity-25 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--page-accent)]"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <path d="m9 18 6-6-6-6" />
            </svg>
          </button>
        </div>
      </footer>
    </div>
  );
}
