/**
 * Reader-view preferences + per-book reading position for the virtualised PDF
 * pane (kinora.md §5.2). Two persisted, framework-light stores in the spirit of
 * {@link ./readingTheme} (a tiny external store + a hook), so the reader returns
 * to the same page at the same zoom:
 *
 * - **view** (global): the {@link FitMode} (fit-width / fit-page / a custom zoom)
 *   and the custom zoom level — how the page is sized in the pane.
 * - **position** (per book): the last page the reader was on, written debounced
 *   as they scroll and restored when they reopen the book.
 */
import { clampZoom, type FitMode } from "@kinora/core";
import { useSyncExternalStore } from "react";

export interface ReaderView {
  fitMode: FitMode;
  /** The zoom factor used when `fitMode === "custom"`. */
  zoom: number;
}

const VIEW_KEY = "kinora.reader.view.v1";
const POSITION_KEY = "kinora.reader.position.v1";

const DEFAULT_VIEW: ReaderView = { fitMode: "width", zoom: 1 };

function loadView(): ReaderView {
  if (typeof localStorage === "undefined") return DEFAULT_VIEW;
  try {
    const raw = JSON.parse(localStorage.getItem(VIEW_KEY) ?? "null");
    if (!raw || typeof raw !== "object") return DEFAULT_VIEW;
    const fitMode: FitMode =
      raw.fitMode === "page" || raw.fitMode === "custom" ? raw.fitMode : "width";
    return { fitMode, zoom: clampZoom(Number(raw.zoom) || 1) };
  } catch {
    return DEFAULT_VIEW;
  }
}

let view = loadView();
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function setView(patch: Partial<ReaderView>): void {
  view = { ...view, ...patch };
  if (patch.zoom !== undefined) view = { ...view, zoom: clampZoom(view.zoom) };
  try {
    localStorage.setItem(VIEW_KEY, JSON.stringify(view));
  } catch {
    /* private mode / quota — keep the in-memory value */
  }
  emit();
}

const subscribe = (listener: () => void): (() => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};

const getSnapshot = (): ReaderView => view;

export interface UseReaderViewResult {
  view: ReaderView;
  /** Switch fit mode (fit-width / fit-page); leaves the custom zoom untouched. */
  setFitMode: (mode: FitMode) => void;
  /** Set an absolute zoom and switch to custom mode (the caller computes it from
   *  the current effective zoom, so zooming out of a fit mode is continuous). */
  setZoom: (zoom: number) => void;
}

/** Read + mutate the persisted reader-view from any reading-room component. */
export function useReaderView(): UseReaderViewResult {
  const current = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  return {
    view: current,
    setFitMode: (mode) => setView({ fitMode: mode }),
    setZoom: (zoom) => setView({ fitMode: "custom", zoom: clampZoom(zoom) }),
  };
}

// --- per-book reading position (debounced) -------------------------------- #

function loadPositions(): Record<string, number> {
  if (typeof localStorage === "undefined") return {};
  try {
    const raw = JSON.parse(localStorage.getItem(POSITION_KEY) ?? "{}");
    return raw && typeof raw === "object" ? (raw as Record<string, number>) : {};
  } catch {
    return {};
  }
}

let positions = loadPositions();
let positionTimer: ReturnType<typeof setTimeout> | null = null;

/** The last page the reader was on for `bookId`, or null if none recorded. */
export function getReadingPosition(bookId: string): number | null {
  const page = positions[bookId];
  return typeof page === "number" && page > 0 ? page : null;
}

/** Record the reader's current page for `bookId` (debounced localStorage write). */
export function setReadingPosition(bookId: string, page: number): void {
  if (!bookId || !(page > 0) || positions[bookId] === page) return;
  positions = { ...positions, [bookId]: page };
  if (positionTimer) clearTimeout(positionTimer);
  positionTimer = setTimeout(() => {
    positionTimer = null;
    try {
      localStorage.setItem(POSITION_KEY, JSON.stringify(positions));
    } catch {
      /* private mode / quota — keep the in-memory value */
    }
  }, 500);
}
