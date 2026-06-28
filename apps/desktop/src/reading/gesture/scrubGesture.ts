// Touch / pointer scrubbing model, pure half. On a touch device (or a trackpad
// drag on the film pane) the reader can scrub the film directly, not only via the
// text scroll. This turns a stream of pointer samples into a scroll-fraction delta
// + a release velocity (for a fling), without any DOM: feed it {x, y, t}; it gives
// back how far to move the playhead and, on release, whether a momentum fling
// should continue and at what decaying velocity. The engine maps the fraction
// delta onto scrollTop (so the existing timeline math is untouched) and runs the
// fling as a short rAF tween.
//
// Orientation: the film pane is vertical (9:16), so a vertical drag scrubs. We
// expose both axes and a configurable dominant axis so the same model works for a
// horizontal filmstrip later.

export type Axis = "x" | "y";

export interface PointerSample {
  x: number;
  y: number;
  /** ms timestamp */
  t: number;
}

export interface ScrubGestureConfig {
  /** which axis drives the scrub (default "y", the vertical film) */
  axis?: Axis;
  /** px of drag that equals the full timeline (default 800 — a deliberate, slow scrub) */
  pxPerFullScrub?: number;
  /** invert direction (drag down = forward vs back); default false = natural (drag up = forward) */
  invert?: boolean;
  /** below this |velocity| (px/s) at release, no fling (default 80) */
  flingMinVelocity?: number;
  /** velocity decay per second for the fling (0..1 retained each second; default 0.0025 ≈ snappy) */
  flingFriction?: number;
}

const DEFAULTS = {
  axis: "y" as Axis,
  pxPerFullScrub: 800,
  invert: false,
  flingMinVelocity: 80,
  flingFriction: 0.0025,
};

export interface ScrubDelta {
  /** fraction of the whole timeline to move (signed; + = forward) */
  fractionDelta: number;
  /** the raw pixel delta on the dominant axis this step */
  pxDelta: number;
}

export interface FlingResult {
  /** should a momentum fling continue after release? */
  fling: boolean;
  /** initial fling velocity in timeline-fractions per second (signed) */
  velocityFractionPerSec: number;
}

/** A small accumulator: begin() on pointer-down, move() per sample, end() on
 *  release. Pure + allocation-light; the engine owns the rAF + scrollTop write. */
export class ScrubGesture {
  private readonly cfg: Required<ScrubGestureConfig>;
  private last: PointerSample | null = null;
  private velPxPerSec = 0; // EMA of release velocity on the dominant axis
  private active = false;

  constructor(config: ScrubGestureConfig = {}) {
    this.cfg = { ...DEFAULTS, ...config };
  }

  get isActive(): boolean {
    return this.active;
  }

  /** Begin a gesture at `sample`. Resets accumulated velocity. */
  begin(sample: PointerSample): void {
    this.active = true;
    this.last = sample;
    this.velPxPerSec = 0;
  }

  /** Feed a move sample; returns the timeline fraction to move since the last
   *  sample. No-op (zero) when not active or on a zero/te time delta. */
  move(sample: PointerSample): ScrubDelta {
    if (!this.active || !this.last) return { fractionDelta: 0, pxDelta: 0 };
    const axis = this.cfg.axis;
    const raw = sample[axis] - this.last[axis];
    const dt = (sample.t - this.last.t) / 1000;
    // Natural scrubbing: drag UP (negative y) advances the film. invert flips it.
    const sign = this.cfg.invert ? 1 : -1;
    const pxDelta = raw * sign;
    if (dt > 0) {
      const inst = pxDelta / dt;
      // Light EMA so a single jittery sample doesn't define the fling.
      this.velPxPerSec = this.velPxPerSec * 0.6 + inst * 0.4;
    }
    this.last = sample;
    return { fractionDelta: pxDelta / this.cfg.pxPerFullScrub, pxDelta };
  }

  /** End the gesture; returns whether a fling should run and its velocity in
   *  timeline-fractions/sec. */
  end(): FlingResult {
    this.active = false;
    const speed = Math.abs(this.velPxPerSec);
    if (speed < this.cfg.flingMinVelocity) {
      this.velPxPerSec = 0;
      return { fling: false, velocityFractionPerSec: 0 };
    }
    const v = this.velPxPerSec / this.cfg.pxPerFullScrub;
    this.velPxPerSec = 0;
    return { fling: true, velocityFractionPerSec: v };
  }

  /** Advance a running fling by `dtSeconds`, returning the fraction to move this
   *  step and the decayed velocity for the next. Stops (returns done) when the
   *  velocity decays below a hair. Pure helper so the rAF tween stays trivial. */
  stepFling(velocityFractionPerSec: number, dtSeconds: number): { fractionDelta: number; velocity: number; done: boolean } {
    const friction = this.cfg.flingFriction;
    // Exponential decay: v *= friction^dt.
    const decay = Math.pow(friction, dtSeconds);
    const fractionDelta = velocityFractionPerSec * dtSeconds;
    const velocity = velocityFractionPerSec * decay;
    const done = Math.abs(velocity) < 1e-3;
    return { fractionDelta, velocity, done };
  }
}
