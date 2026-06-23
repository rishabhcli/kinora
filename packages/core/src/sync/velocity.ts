/**
 * Reading-velocity estimate in words/second, smoothed with an EWMA whose weight
 * is derived from a half-life so it's frame-rate independent. Feeds the
 * Scheduler's intent updates (velocity-adaptive promotion, §4.6). Pure and
 * deterministic — every entry point takes an explicit `nowMs`.
 */

const DEFAULT_HALF_LIFE_MS = 2000;
const DEFAULT_MIN_WPS = 0;
const DEFAULT_MAX_WPS = 25;

export interface VelocityOptions {
  /** Half-life of the EWMA, ms. Larger = smoother/slower to react. */
  halfLifeMs?: number;
  /** Clamp floor (backward reading is treated as 0). */
  minWps?: number;
  /** Clamp ceiling (guards against teleport/seek spikes). */
  maxWps?: number;
}

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, value));
}

export class VelocityTracker {
  private lastWord: number | null = null;
  private lastMs: number | null = null;
  private ewma = 0;
  private readonly halfLifeMs: number;
  private readonly minWps: number;
  private readonly maxWps: number;

  constructor(opts: VelocityOptions = {}) {
    this.halfLifeMs = opts.halfLifeMs ?? DEFAULT_HALF_LIFE_MS;
    this.minWps = opts.minWps ?? DEFAULT_MIN_WPS;
    this.maxWps = opts.maxWps ?? DEFAULT_MAX_WPS;
  }

  /** Record the focus word at `nowMs`; returns the current smoothed wps. */
  sample(word: number, nowMs: number): number {
    if (this.lastWord === null || this.lastMs === null) {
      this.lastWord = word;
      this.lastMs = nowMs;
      return this.value;
    }
    const dtMs = nowMs - this.lastMs;
    if (dtMs <= 0) {
      this.lastWord = word;
      return this.value;
    }
    const instantaneous = clamp(
      ((word - this.lastWord) / dtMs) * 1000,
      this.minWps,
      this.maxWps,
    );
    const alpha = 1 - Math.pow(0.5, dtMs / this.halfLifeMs);
    this.ewma += alpha * (instantaneous - this.ewma);
    this.lastWord = word;
    this.lastMs = nowMs;
    return this.value;
  }

  get value(): number {
    return clamp(this.ewma, this.minWps, this.maxWps);
  }

  reset(): void {
    this.lastWord = null;
    this.lastMs = null;
    this.ewma = 0;
  }
}
