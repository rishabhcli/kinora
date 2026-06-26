import { useCallback, useRef } from "react";

/**
 * useSharedElement — the FLIP plumbing behind shared-element morphs.
 *
 * A shared-element transition animates one logical object (a book cover)
 * between two positions in the layout (its shelf slot ↔ the reading room)
 * using only transforms. These helpers capture rects and compute the
 * First-Last-Invert-Play transform between them — pure geometry, no React
 * state on the hot path.
 */

export interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Viewport-relative rect of a DOM element. */
export function getRect(el: Element): Rect {
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}

/**
 * The transform that makes an element currently laid out at `to` appear
 * as if it were at `from` (so animating the transform → identity plays
 * the morph). Uniform scale off the width ratio keeps the cover's aspect.
 */
export function flipFrom(from: Rect, to: Rect): { x: number; y: number; scale: number } {
  const scale = to.width > 0 ? from.width / to.width : 1;
  const fromCx = from.left + from.width / 2;
  const fromCy = from.top + from.height / 2;
  const toCx = to.left + to.width / 2;
  const toCy = to.top + to.height / 2;
  return { x: fromCx - toCx, y: fromCy - toCy, scale };
}

/**
 * The centred "hero" box that matches the reading room's opened cover
 * (width: min(40vh, 300px), aspect 2/3). The travel morph lands here so
 * the hand-off to the room's own hinge is seamless.
 */
export function heroCoverRect(): Rect {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const width = Math.min(vh * 0.4, 300);
  const height = width * 1.5;
  return { left: (vw - width) / 2, top: (vh - height) / 2, width, height };
}

const COVER_SELECTOR = ".book-cover, [data-book-cover], [data-shared-cover]";

/**
 * Find the on-shelf cover rect from a click/pointer event, walking up to
 * the nearest cover element. Lets Agent 04 capture the origin rect from
 * HomePage WITHOUT modifying Agent 5's BookCard markup — it keys off the
 * existing `.book-cover` class (or an opt-in data attribute).
 */
export function coverRectFromEvent(e: { target: EventTarget | null }): Rect | null {
  const node = e.target as HTMLElement | null;
  if (!node || typeof node.closest !== "function") return null;
  const card = node.closest<HTMLElement>(COVER_SELECTOR);
  return card ? getRect(card) : null;
}

/**
 * Hook form: remembers the last cover rect captured from a pointerdown so
 * a subsequent `onOpen(book)` (which carries no rect) can still morph.
 */
export function useSharedElement() {
  const lastRect = useRef<Rect | null>(null);

  const capturePointer = useCallback((e: { target: EventTarget | null }) => {
    const r = coverRectFromEvent(e);
    if (r) lastRect.current = r;
  }, []);

  const takeRect = useCallback((): Rect | null => {
    const r = lastRect.current;
    lastRect.current = null;
    return r;
  }, []);

  return { capturePointer, takeRect, getRect, flipFrom, heroCoverRect };
}
