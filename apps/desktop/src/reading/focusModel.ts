// Pure, DOM-free focus math for the Scroll Film Engine's scroll-paint hot path.
// Extracted from ScrollFilmEngine so it is unit-testable and so the engine's rAF
// loop reads only cached arrays (never layout) per frame. The "gentle focus" ramp
// replaces the old hard two-state 1.0 / 0.62 dim with a soft, Apple-Books-calm
// falloff that keeps off-focus paragraphs comfortably readable.

const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

/** The content-space Y (px, relative to the scroll content's top) of the focus
 *  line — the line a paragraph must cross to become "active". `focusRatio` is the
 *  fraction down the viewport (0.4 = 40%, matching the prior behaviour). */
export function focusContentY(scrollTop: number, clientHeight: number, focusRatio = 0.4): number {
  return scrollTop + clientHeight * focusRatio;
}

/** The active paragraph index: the greatest index whose cached content-top is at
 *  or above the focus line. `tops` MUST be ascending (document order). Returns 0
 *  for an empty array (defensive) or when nothing has crossed the line yet. */
export function activeParagraphIndex(tops: number[], focusY: number): number {
  let best = 0;
  for (let i = 0; i < tops.length; i++) {
    if (tops[i] <= focusY) best = i;
    else break; // ascending → no later top can be ≤ focusY
  }
  return best;
}

export interface FocusOpacityOpts {
  /** the floor opacity far-from-focus paragraphs settle to (default 0.78) */
  min?: number;
  /** how many paragraphs away reach the floor (default 2) */
  falloff?: number;
}

/** A gentle opacity for a paragraph `distance` rows from the active one. The
 *  active paragraph (distance 0) is fully opaque; opacity ramps softly toward
 *  `min` over `falloff` rows and stays at `min` beyond. Direction-agnostic. */
export function focusOpacity(distanceFromActive: number, opts: FocusOpacityOpts = {}): number {
  const min = opts.min ?? 0.78;
  const falloff = Math.max(1, opts.falloff ?? 2);
  const d = Math.abs(distanceFromActive);
  if (d === 0) return 1;
  const t = clamp(d / falloff, 0, 1);
  return 1 - t * (1 - min);
}
