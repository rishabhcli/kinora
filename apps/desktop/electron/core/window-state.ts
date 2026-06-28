/**
 * Window-state persistence math — pure, Electron-free, unit-testable.
 *
 * The window manager persists each window's bounds + maximised/fullscreen flags
 * so a relaunch restores the layout. Restoring naively is dangerous: a monitor
 * may have been unplugged, resolution may have shrunk, or a stored size may be
 * absurd. This module owns the *decisions* (clamp into a visible display, fall
 * back to a sane default) as pure functions over plain data; the Electron-bound
 * code just feeds it `screen` display bounds and applies the result.
 */

export interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface WindowState {
  bounds: Bounds;
  maximized: boolean;
  fullScreen: boolean;
  /** The `id` the renderer route should hydrate (multi-window). */
  displayId?: number;
}

/** A rectangle as reported by Electron's `screen.getAllDisplays()[].workArea`. */
export interface DisplayRect {
  id: number;
  bounds: Bounds;
}

export interface WindowConstraints {
  minWidth: number;
  minHeight: number;
  defaultWidth: number;
  defaultHeight: number;
}

export const DEFAULT_CONSTRAINTS: WindowConstraints = {
  minWidth: 900,
  minHeight: 600,
  defaultWidth: 1280,
  defaultHeight: 800,
};

/** A window must overlap a visible display by at least this many px to count. */
const MIN_VISIBLE_OVERLAP = 80;

/**
 * Validate a persisted {@link WindowState} against the current displays and
 * return a state that is guaranteed visible & sane. If the stored window is
 * fully off-screen (monitor removed), it is re-centred on the primary display.
 */
export function reconcileWindowState(
  stored: Partial<WindowState> | null | undefined,
  displays: readonly DisplayRect[],
  constraints: WindowConstraints = DEFAULT_CONSTRAINTS,
): WindowState {
  const primary = displays[0]?.bounds ?? syntheticPrimary(constraints);

  if (!stored || !isValidBounds(stored.bounds)) {
    return {
      bounds: centeredDefault(primary, constraints),
      maximized: Boolean(stored?.maximized),
      fullScreen: Boolean(stored?.fullScreen),
    };
  }

  // Clamp the size first so an oversized stored window can't escape the display.
  const sized = clampSize(stored.bounds, displays, constraints);

  // If it no longer overlaps any display, re-centre on primary.
  const visible = displays.some((d) => overlapArea(sized, d.bounds) >= visibleThreshold(sized));
  const bounds = visible ? sized : centeredDefault(primary, sized);

  return {
    bounds,
    maximized: Boolean(stored.maximized),
    fullScreen: Boolean(stored.fullScreen),
    displayId: stored.displayId,
  };
}

/** Clamp a width/height to the largest display and to the min constraints. */
export function clampSize(
  bounds: Bounds,
  displays: readonly DisplayRect[],
  constraints: WindowConstraints,
): Bounds {
  const maxW = Math.max(constraints.minWidth, ...displays.map((d) => d.bounds.width));
  const maxH = Math.max(constraints.minHeight, ...displays.map((d) => d.bounds.height));
  return {
    x: Math.round(bounds.x),
    y: Math.round(bounds.y),
    width: clamp(Math.round(bounds.width), constraints.minWidth, maxW || constraints.defaultWidth),
    height: clamp(Math.round(bounds.height), constraints.minHeight, maxH || constraints.defaultHeight),
  };
}

/** Center `size`'s width/height within `area`. */
export function centeredDefault(area: Bounds, size: Pick<Bounds, "width" | "height"> | WindowConstraints): Bounds {
  const width = "width" in size ? size.width : size.defaultWidth;
  const height = "height" in size ? size.height : size.defaultHeight;
  return {
    width,
    height,
    x: Math.round(area.x + (area.width - width) / 2),
    y: Math.round(area.y + (area.height - height) / 2),
  };
}

/**
 * Offset a new window so it cascades off an existing one rather than landing
 * exactly on top — clamped so the cascade stays on-screen.
 */
export function cascadeFrom(base: Bounds, area: Bounds, step = 28): Bounds {
  let x = base.x + step;
  let y = base.y + step;
  // If the cascade would push the window off the right/bottom, wrap to the
  // display origin with a small inset.
  if (x + base.width > area.x + area.width) x = area.x + step;
  if (y + base.height > area.y + area.height) y = area.y + step;
  return { x, y, width: base.width, height: base.height };
}

export function isValidBounds(b: Partial<Bounds> | null | undefined): b is Bounds {
  return (
    !!b &&
    Number.isFinite(b.x) &&
    Number.isFinite(b.y) &&
    Number.isFinite(b.width) &&
    Number.isFinite(b.height) &&
    (b.width as number) > 0 &&
    (b.height as number) > 0
  );
}

// --- internal helpers -------------------------------------------------------

function visibleThreshold(b: Bounds): number {
  // Require either a fixed minimum overlap or 5% of the window area, whichever
  // is smaller — so tiny windows aren't impossible to keep on-screen.
  return Math.min(MIN_VISIBLE_OVERLAP * MIN_VISIBLE_OVERLAP, b.width * b.height * 0.05);
}

function overlapArea(a: Bounds, b: Bounds): number {
  const x = Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x));
  const y = Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y));
  return x * y;
}

function clamp(v: number, lo: number, hi: number): number {
  if (hi < lo) return lo;
  return Math.max(lo, Math.min(hi, v));
}

function syntheticPrimary(c: WindowConstraints): Bounds {
  return { x: 0, y: 0, width: Math.max(c.defaultWidth, 1024), height: Math.max(c.defaultHeight, 768) };
}
