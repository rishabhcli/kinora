/**
 * The reading-line focus-word resolver (kinora.md §4.3) for the virtualised PDF
 * reader (§5.2). The page pane renders real rasterised pages with each word's
 * normalised box overlaid; as the reader scrolls, the SyncEngine needs the
 * **focus word `w`** — "the word nearest the reading line — the top third of the
 * viewport, not the centre, because eyes lead the scroll."
 *
 * This is the pure selection over the pages currently laid out in the viewport.
 * The view supplies each page's rendered vertical extent (in one shared
 * scroll-content coordinate space) and its word boxes; given the reading-line Y
 * in that same space, we return the global `word_index` closest to it. No DOM,
 * no state — exhaustively unit-testable; the desktop scroll-spy feeds it
 * measured rects.
 */

/** A word's normalised box on a rendered page: `[x, y, w, h]` in `[0, 1]` (§9.4). */
export interface PageWordBox {
  word_index: number;
  bbox: readonly number[];
}

/** A page laid out in the scroll viewport: its rendered image extent + words. */
export interface RenderedPage {
  page: number;
  /** Top of the rendered page image, in scroll-content coordinates. */
  top: number;
  /** Rendered height of the page image, in scroll-content coordinates. */
  height: number;
  words: readonly PageWordBox[];
}

/**
 * The focus word `w`: the word whose vertical centre is nearest `readingLineY`
 * across every laid-out page. Returns its global `word_index`, or `null` when no
 * page carries words (e.g. a full-page illustration on screen).
 *
 * Words on the same line share an identical centre, so ties break toward the
 * smaller `word_index` — i.e. the start of the line in reading order — which
 * keeps `w` advancing monotonically as lines cross the reading line.
 */
export function focusWordAtReadingLine(
  pages: readonly RenderedPage[],
  readingLineY: number,
): number | null {
  let bestIndex: number | null = null;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const page of pages) {
    if (page.height <= 0) continue;
    for (const word of page.words) {
      const y = word.bbox[1] ?? 0;
      const h = word.bbox[3] ?? 0;
      const centreY = page.top + (y + h / 2) * page.height;
      const dist = Math.abs(centreY - readingLineY);
      if (dist < bestDist || (dist === bestDist && (bestIndex === null || word.word_index < bestIndex))) {
        bestDist = dist;
        bestIndex = word.word_index;
      }
    }
  }
  return bestIndex;
}

/** The on-screen rect of a contain-fit image. */
export interface FitRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * The displayed rect of a `contain`-fit image (intrinsic aspect `ratio` = w / h)
 * centred in a `cw × ch` box — the geometry the mobile raster pane needs to map
 * normalised word boxes to where the pixels actually land (and back, for taps).
 */
export function containRect(cw: number, ch: number, ratio: number): FitRect {
  if (cw <= 0 || ch <= 0 || ratio <= 0) return { x: 0, y: 0, w: 0, h: 0 };
  let w = cw;
  let h = cw / ratio;
  if (h > ch) {
    h = ch;
    w = ch * ratio;
  }
  return { x: (cw - w) / 2, y: (ch - h) / 2, w, h };
}

/**
 * Tap-to-seek hit-testing on the raster: the word at a normalised page point
 * `(nx, ny)` in `[0, 1]` — the word whose box contains it, else the word with
 * the nearest centre. Returns its global `word_index`, or null when there are
 * no words (a full-page illustration).
 */
export function wordAtNormalisedPoint(
  words: readonly PageWordBox[],
  nx: number,
  ny: number,
): number | null {
  let best: number | null = null;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const word of words) {
    const x = word.bbox[0] ?? 0;
    const y = word.bbox[1] ?? 0;
    const w = word.bbox[2] ?? 0;
    const h = word.bbox[3] ?? 0;
    if (nx >= x && nx <= x + w && ny >= y && ny <= y + h) return word.word_index;
    const dx = nx - (x + w / 2);
    const dy = ny - (y + h / 2);
    const dist = dx * dx + dy * dy;
    if (dist < bestDist) {
      bestDist = dist;
      best = word.word_index;
    }
  }
  return best;
}

// --- reader view geometry (virtualised PDF pane, §5.2) --------------------- #

/** How the page is sized in the pane: fill the width, fit the whole page, or a
 *  reader-chosen zoom. */
export type FitMode = "width" | "page" | "custom";

export const ZOOM_MIN = 0.5;
export const ZOOM_MAX = 4;

/** Clamp a zoom factor to the supported range (and guard NaN/∞). */
export function clampZoom(zoom: number): number {
  if (!Number.isFinite(zoom)) return 1;
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom));
}

/**
 * The page sitting at the reading line — the smallest vertical gap to it across
 * the laid-out pages (0 when the line is inside the page). Drives the page
 * readout and the persisted reading position. Null when nothing is laid out.
 */
export function visiblePageAtLine(
  pages: readonly RenderedPage[],
  readingLineY: number,
): number | null {
  let best: number | null = null;
  let bestDelta = Number.POSITIVE_INFINITY;
  for (const page of pages) {
    const bottom = page.top + page.height;
    const delta =
      readingLineY < page.top
        ? page.top - readingLineY
        : readingLineY > bottom
          ? readingLineY - bottom
          : 0;
    if (delta < bestDelta) {
      bestDelta = delta;
      best = page.page;
    }
  }
  return best;
}

/**
 * The zoom factor for a {@link FitMode}. `baseWidth` is the fit-to-width surface
 * width at zoom 1 and `ratio` the page's width/height. "width" is the base fit
 * (1×); "page" shrinks the page so its full height fits the pane (manga/full-page
 * art); "custom" uses the reader's chosen zoom. Always clamped.
 */
export function fitZoom(
  mode: FitMode,
  pane: { width: number; height: number },
  ratio: number,
  baseWidth: number,
  customZoom: number,
): number {
  if (mode === "custom") return clampZoom(customZoom);
  if (mode === "width" || baseWidth <= 0 || ratio <= 0 || pane.height <= 0) return 1;
  const vpad = 32; // the row's vertical padding, excluded from the page box
  const fitPageWidth = Math.max(1, (pane.height - vpad) * ratio);
  return clampZoom(fitPageWidth / baseWidth);
}

/**
 * Page numbers (1-based) to prefetch around `visiblePage`, within `[1, count]`,
 * nearest-first so the immediately-adjacent pages decode before the far ones —
 * keeping fast scrolling from showing blank pages (§5.2 "a few seconds ahead").
 */
export function prefetchRange(visiblePage: number, count: number, radius: number): number[] {
  const out: number[] = [];
  for (let d = 1; d <= radius; d++) {
    const next = visiblePage + d;
    const prev = visiblePage - d;
    if (next >= 1 && next <= count) out.push(next);
    if (prev >= 1 && prev <= count) out.push(prev);
  }
  return out;
}
