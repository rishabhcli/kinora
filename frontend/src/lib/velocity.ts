import { clamp } from "./math";

// Reading-velocity model (kinora.md §4.3). `v` is an exponentially-weighted
// moving average of words/sec over a ~10s window, clamped to [0.5×, 3×] of a
// 4 wps (≈240 wpm) default. Clamping is what stops a single flick of the
// trackpad from spiking the estimate.
export const DEFAULT_WPS = 4;
export const MIN_VELOCITY_FACTOR = 0.5;
export const MAX_VELOCITY_FACTOR = 3;
export const EWMA_WINDOW_MS = 10_000;

export function velocityBounds(defaultWps: number = DEFAULT_WPS): {
  min: number;
  max: number;
} {
  return {
    min: MIN_VELOCITY_FACTOR * defaultWps,
    max: MAX_VELOCITY_FACTOR * defaultWps,
  };
}

/** Time-aware EWMA smoothing factor: α = 1 − e^(−Δt / window). */
export function ewmaAlpha(dtMs: number, windowMs: number = EWMA_WINDOW_MS): number {
  if (dtMs <= 0) return 0;
  return 1 - Math.exp(-dtMs / windowMs);
}

export interface VelocityOptions {
  defaultWps?: number;
  windowMs?: number;
}

/**
 * Tracks reading velocity from (focus word, time) samples. The reported value
 * is always clamped to the bounds; direction (forward / backward) is tracked
 * separately because a backward seek simply re-targets the buffer behind `w`.
 */
export class VelocityTracker {
  private readonly defaultWps: number;
  private readonly windowMs: number;
  private readonly min: number;
  private readonly max: number;
  private ewma: number;
  private lastWord: number | null = null;
  private lastTimeMs: number | null = null;
  private lastDirection: 1 | -1 = 1;

  constructor(opts: VelocityOptions = {}) {
    this.defaultWps = opts.defaultWps ?? DEFAULT_WPS;
    this.windowMs = opts.windowMs ?? EWMA_WINDOW_MS;
    const bounds = velocityBounds(this.defaultWps);
    this.min = bounds.min;
    this.max = bounds.max;
    this.ewma = this.defaultWps;
  }

  /** Feed a new (focus word, time) sample; returns the clamped velocity. */
  sample(word: number, timeMs: number): number {
    if (this.lastWord === null || this.lastTimeMs === null) {
      this.lastWord = word;
      this.lastTimeMs = timeMs;
      return this.value;
    }
    const dtMs = timeMs - this.lastTimeMs;
    if (dtMs <= 0) {
      // Same instant / out-of-order sample: re-anchor the word, keep velocity.
      this.lastWord = word;
      return this.value;
    }
    const dWords = word - this.lastWord;
    if (dWords !== 0) this.lastDirection = dWords > 0 ? 1 : -1;
    const instantaneous = Math.abs(dWords) / (dtMs / 1000); // wps magnitude
    const alpha = ewmaAlpha(dtMs, this.windowMs);
    this.ewma = clamp(this.ewma + alpha * (instantaneous - this.ewma), this.min, this.max);
    this.lastWord = word;
    this.lastTimeMs = timeMs;
    return this.value;
  }

  /** Current clamped reading velocity in words/sec. */
  get value(): number {
    return clamp(this.ewma, this.min, this.max);
  }

  get direction(): 1 | -1 {
    return this.lastDirection;
  }

  /** §4.8 — on a far seek, velocity resets to the default until fresh samples. */
  reset(): void {
    this.ewma = this.defaultWps;
    this.lastWord = null;
    this.lastTimeMs = null;
    this.lastDirection = 1;
  }
}
