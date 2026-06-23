/**
 * The client-side Ken-Burns pan (§4.4, §12.4). When a beat has only a still —
 * a generated keyframe or the book's own illustration — both shells animate a
 * slow zoom/drift over it in CSS / RN `Animated`, at **zero generation cost**,
 * so a speculative beat reads as "a slow establishing shot" instead of a spinner.
 *
 * The pan is chosen *deterministically from the beat id* so (a) the two shells
 * agree and (b) re-reading a passage replays the identical motion — calm, never
 * random. Translations are fractions of the frame (applied after the zoom).
 */

export interface KenBurnsPreset {
  fromScale: number;
  toScale: number;
  /** Drift, as a fraction of the frame, applied as translate() after scale(). */
  fromX: number;
  toX: number;
  fromY: number;
  toY: number;
  /** A full one-way sweep, seconds — long enough to hold a still calmly. */
  durationS: number;
}

/**
 * Four gentle moves: a slow push-in, a pull-out, and two cross-drifts. Every one
 * keeps the still over-scaled throughout so an edge never exposes during the pan.
 */
const PRESETS: readonly KenBurnsPreset[] = [
  { fromScale: 1.02, toScale: 1.14, fromX: -0.02, toX: 0.025, fromY: -0.015, toY: 0.02, durationS: 18 },
  { fromScale: 1.14, toScale: 1.03, fromX: 0.03, toX: -0.02, fromY: 0.02, toY: -0.015, durationS: 20 },
  { fromScale: 1.05, toScale: 1.13, fromX: 0.025, toX: -0.025, fromY: 0.01, toY: -0.01, durationS: 17 },
  { fromScale: 1.12, toScale: 1.04, fromX: -0.03, toX: 0.02, fromY: -0.02, toY: 0.015, durationS: 19 },
];

/** A small, stable FNV-ish hash so the same seed always picks the same preset. */
function hash(seed: string): number {
  let h = 2166136261;
  for (let i = 0; i < seed.length; i++) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** Pick the deterministic Ken-Burns move for a beat/still (stable across shells). */
export function kenBurnsPreset(seed: string | null | undefined): KenBurnsPreset {
  const presets = PRESETS;
  const index = seed ? hash(seed) % presets.length : 0;
  return presets[index] as KenBurnsPreset;
}

export const KEN_BURNS_PRESET_COUNT = PRESETS.length;

/** Reading speed (words/sec) at/above which the pan freezes — a skim, not a dwell. */
export const KEN_BURNS_FREEZE_WPS = 12;

/** How the pan should behave for the reader's current pace (§4.6). */
export interface KenBurnsTempo {
  /** Hold the still — the reader is skimming too fast for motion to register. */
  paused: boolean;
  /** Multiply the base pan duration; >1 = a slower, calmer drift as pace quickens. */
  durationScale: number;
}

/**
 * Velocity-adaptive Ken-Burns (§4.6). A dwelling reader gets the full, lively pan
 * (they're savouring the moment); as the pace quickens the drift slows and calms
 * so a wall of stills flashing past isn't a jitter of restarting zooms; past the
 * skim threshold the pan freezes entirely (motion no one can appreciate is just
 * noise — and it saves the compositor work while the reader sprints).
 */
export function kenBurnsTempo(velocity: number): KenBurnsTempo {
  const v = Number.isFinite(velocity) ? Math.max(0, velocity) : 0;
  if (v >= KEN_BURNS_FREEZE_WPS) return { paused: true, durationScale: 1 };
  // 1.0× at a calm reading pace, ramping to ~2.2× (slower) toward the freeze point.
  const durationScale = 1 + Math.min(v / KEN_BURNS_FREEZE_WPS, 1) * 1.2;
  return { paused: false, durationScale };
}
