// Performance instrumentation for the Scroll Film Engine — the pure, DOM-free
// half. Accumulates frame timings into a rolling window and derives the metrics
// §12.5 calls for (frame budget adherence, jank, dropped frames, percentiles)
// without ever touching `performance` or the DOM, so the whole thing is unit-
// testable by feeding it synthetic timestamps. A thin DOM adapter (usePerfMonitor)
// feeds it real rAF timestamps.
//
// The model: each rendered frame has a *duration* = the gap since the previous
// frame's timestamp. At a 60Hz refresh the budget is ~16.67ms; a frame that
// overruns it is "over budget", and a frame that overruns it badly (> a jank
// multiple) is "janky" — a user-visible hitch. We keep a fixed-size ring of the
// most recent durations so the stats reflect *recent* behaviour (a flick that
// stuttered five seconds ago shouldn't taint the current reading), and we keep
// running totals so lifetime aggregates are O(1).

/** A single performance sample: a frame and (optionally) what produced it. */
export interface FrameSample {
  /** frame duration in ms (gap since the previous frame's timestamp) */
  durationMs: number;
  /** the engine's mode for this frame, if known (for per-mode breakdowns) */
  mode?: "scrub" | "play";
}

export interface FrameStatsConfig {
  /** target frame budget in ms; default 60Hz ≈ 16.67ms */
  budgetMs?: number;
  /** a frame longer than `budgetMs * jankFactor` is a user-visible hitch */
  jankFactor?: number;
  /** how many recent frames the rolling window keeps (default 240 ≈ 4s @60fps) */
  windowSize?: number;
}

export interface FrameStatsSnapshot {
  /** frames recorded in the rolling window */
  count: number;
  /** frames per second over the window (count / windowMs * 1000), 0 when empty */
  fps: number;
  /** mean frame duration over the window (ms) */
  meanMs: number;
  /** 95th-percentile frame duration over the window (ms) — the "feel" metric */
  p95Ms: number;
  /** worst single frame in the window (ms) */
  maxMs: number;
  /** fraction [0,1] of windowed frames that exceeded the budget */
  overBudgetRatio: number;
  /** count of windowed frames that exceeded the jank threshold */
  jankCount: number;
  /** windowed jank frames as a fraction [0,1] */
  jankRatio: number;
  /** approximate dropped frames in the window: Σ (floor(duration/budget) − 1) */
  droppedFrames: number;
  /** lifetime totals (not windowed) — for the observability panel / logging */
  lifetime: { frames: number; jank: number; dropped: number };
}

const DEFAULT_BUDGET_MS = 1000 / 60;
const DEFAULT_JANK_FACTOR = 2; // > 2 budgets late = a hitch a reader notices
const DEFAULT_WINDOW = 240;

/** A bounded ring of recent frame durations with O(1) push and lifetime totals.
 *  Pure: no `performance`, no DOM. Feed it durations; read derived stats. */
export class FrameStats {
  private readonly budgetMs: number;
  private readonly jankThresholdMs: number;
  private readonly windowSize: number;
  private readonly ring: number[];
  private head = 0; // next write index
  private filled = 0; // entries written so far (≤ windowSize)
  // Lifetime aggregates (survive ring eviction).
  private lifeFrames = 0;
  private lifeJank = 0;
  private lifeDropped = 0;

  constructor(config: FrameStatsConfig = {}) {
    this.budgetMs = config.budgetMs && config.budgetMs > 0 ? config.budgetMs : DEFAULT_BUDGET_MS;
    const jf = config.jankFactor && config.jankFactor > 1 ? config.jankFactor : DEFAULT_JANK_FACTOR;
    this.jankThresholdMs = this.budgetMs * jf;
    this.windowSize =
      config.windowSize && config.windowSize > 0 ? Math.floor(config.windowSize) : DEFAULT_WINDOW;
    this.ring = new Array<number>(this.windowSize);
  }

  /** Record one frame duration (ms). Non-finite or negative values are ignored
   *  so a stale rAF delta (e.g. the loop resuming after a tab was backgrounded)
   *  can't poison the stats. */
  record(durationMs: number): void {
    if (!Number.isFinite(durationMs) || durationMs < 0) return;
    this.ring[this.head] = durationMs;
    this.head = (this.head + 1) % this.windowSize;
    if (this.filled < this.windowSize) this.filled++;
    this.lifeFrames++;
    if (durationMs > this.jankThresholdMs) this.lifeJank++;
    if (durationMs > this.budgetMs) {
      // A 33ms frame at a 16.67ms budget "dropped" one frame; a 50ms one dropped two.
      this.lifeDropped += Math.max(0, Math.floor(durationMs / this.budgetMs) - 1);
    }
  }

  /** Convenience: record a {@link FrameSample}. */
  sample(s: FrameSample): void {
    this.record(s.durationMs);
  }

  /** True once at least one frame has been recorded. */
  get hasData(): boolean {
    return this.filled > 0;
  }

  /** Reset the rolling window AND lifetime totals (book change / unmount). */
  reset(): void {
    this.head = 0;
    this.filled = 0;
    this.lifeFrames = 0;
    this.lifeJank = 0;
    this.lifeDropped = 0;
  }

  /** Snapshot every derived metric over the current window + lifetime. O(n) in
   *  the window size; called at a low cadence (e.g. 2Hz) by the DOM adapter, not
   *  per frame. */
  snapshot(): FrameStatsSnapshot {
    const n = this.filled;
    if (n === 0) {
      return {
        count: 0,
        fps: 0,
        meanMs: 0,
        p95Ms: 0,
        maxMs: 0,
        overBudgetRatio: 0,
        jankCount: 0,
        jankRatio: 0,
        droppedFrames: 0,
        lifetime: { frames: 0, jank: 0, dropped: 0 },
      };
    }
    const sorted = this.toArray();
    let sum = 0;
    let over = 0;
    let jank = 0;
    let dropped = 0;
    let max = 0;
    for (const d of sorted) {
      sum += d;
      if (d > this.budgetMs) {
        over++;
        dropped += Math.max(0, Math.floor(d / this.budgetMs) - 1);
      }
      if (d > this.jankThresholdMs) jank++;
      if (d > max) max = d;
    }
    sorted.sort((a, b) => a - b);
    const mean = sum / n;
    const windowMs = sum; // total elapsed across the window
    return {
      count: n,
      fps: windowMs > 0 ? (n / windowMs) * 1000 : 0,
      meanMs: mean,
      p95Ms: percentile(sorted, 0.95),
      maxMs: max,
      overBudgetRatio: over / n,
      jankCount: jank,
      jankRatio: jank / n,
      droppedFrames: dropped,
      lifetime: { frames: this.lifeFrames, jank: this.lifeJank, dropped: this.lifeDropped },
    };
  }

  /** The live window contents oldest→newest (mostly for tests / debugging). */
  toArray(): number[] {
    const out: number[] = [];
    if (this.filled < this.windowSize) {
      for (let i = 0; i < this.filled; i++) out.push(this.ring[i]);
    } else {
      for (let i = 0; i < this.windowSize; i++) out.push(this.ring[(this.head + i) % this.windowSize]);
    }
    return out;
  }
}

/** Linear-interpolated percentile of an already-sorted ascending array.
 *  `q` in [0,1]. Empty → 0. */
export function percentile(sortedAsc: readonly number[], q: number): number {
  const n = sortedAsc.length;
  if (n === 0) return 0;
  if (n === 1) return sortedAsc[0];
  const clampedQ = q < 0 ? 0 : q > 1 ? 1 : q;
  const rank = clampedQ * (n - 1);
  const lo = Math.floor(rank);
  const hi = Math.ceil(rank);
  if (lo === hi) return sortedAsc[lo];
  const frac = rank - lo;
  return sortedAsc[lo] * (1 - frac) + sortedAsc[hi] * frac;
}
