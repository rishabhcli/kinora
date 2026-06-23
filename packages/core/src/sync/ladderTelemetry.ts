/**
 * Observability for the §12.4 degradation ladder (the §12.5 per-session metrics
 * and the §13 "latency-to-first-frame on seek" number). It is a passive observer
 * of the SyncEngine's snapshot stream — it instruments **nothing** inside the
 * engine, so it adds no coupling and no render churn: a shell `attach()`es it (or
 * a test drives `observe()` directly with crafted snapshots and an injected clock).
 *
 * What it measures, purely from the snapshot:
 * - **time-in-rung** — how long the film spent on each ladder rung (the buffer-
 *   health story: a high `fullVideoFraction` means the reader mostly saw real film);
 * - **transitions / stalls** — ladder churn, and entries into the bare audio-text
 *   floor (the only "nothing on screen" rung — a visible stall);
 * - **latency-to-first-frame on seek** — wall-clock from a seek (detected via the
 *   engine's monotonically-increasing `playheadSeekSeq`) to the next coherent
 *   frame (any visual rung). With the keyframe bridge this should be ~one tick.
 */
import type { BeatStage } from "./SyncEngine";

export interface LadderMetrics {
  /** Number of rung changes over the session. */
  transitions: number;
  /** Cumulative milliseconds spent on each rung (current rung included, live). */
  msInRung: Record<BeatStage, number>;
  /** Entries into the bare `audio_text_only` floor — the only true visible stall. */
  stalls: number;
  /** Most recent seek→first-coherent-frame latency, ms (null until a seek lands). */
  lastSeekToFirstFrameMs: number | null;
  /** Worst seek→first-frame latency seen, ms (the §13 tail to keep near zero). */
  maxSeekToFirstFrameMs: number | null;
  /** Fraction of watched time spent on real video — the headline buffer-health %. */
  fullVideoFraction: number;
}

/** The minimal snapshot shape the telemetry reads (a subset of `SyncSnapshot`). */
export interface LadderObservable {
  currentStage: BeatStage;
  playheadSeekSeq: number;
}

const ZERO_RUNGS = (): Record<BeatStage, number> => ({
  full_video: 0,
  keyframe_ken_burns: 0,
  illustration: 0,
  audio_text_only: 0,
});

export class LadderTelemetry {
  private readonly now: () => number;
  private lastStage: BeatStage | null = null;
  private stageSince: number;
  private lastSeekSeq = 0;
  private pendingSeekAt: number | null = null;
  private transitions = 0;
  private stalls = 0;
  private readonly msInRung = ZERO_RUNGS();
  private lastSeekToFirstFrameMs: number | null = null;
  private maxSeekToFirstFrameMs: number | null = null;

  constructor(opts: { now?: () => number } = {}) {
    this.now = opts.now ?? (() => Date.now());
    this.stageSince = this.now();
  }

  /** Fold one snapshot into the running metrics. Idempotent on an unchanged snapshot. */
  observe(s: LadderObservable): void {
    const t = this.now();

    // A seek bumped the monotonic sequence — start timing to the next coherent frame.
    if (s.playheadSeekSeq !== this.lastSeekSeq) {
      this.lastSeekSeq = s.playheadSeekSeq;
      this.pendingSeekAt = t;
    }

    // Accumulate dwell on the rung we are leaving; count the transition + stall.
    if (this.lastStage === null) {
      this.lastStage = s.currentStage;
      this.stageSince = t;
    } else if (s.currentStage !== this.lastStage) {
      this.msInRung[this.lastStage] += t - this.stageSince;
      this.stageSince = t;
      this.transitions += 1;
      if (s.currentStage === "audio_text_only") this.stalls += 1;
      this.lastStage = s.currentStage;
    }

    // The first visual rung after a seek closes the latency measurement.
    if (this.pendingSeekAt !== null && s.currentStage !== "audio_text_only") {
      const latency = t - this.pendingSeekAt;
      this.lastSeekToFirstFrameMs = latency;
      this.maxSeekToFirstFrameMs = Math.max(this.maxSeekToFirstFrameMs ?? 0, latency);
      this.pendingSeekAt = null;
    }
  }

  /** Subscribe to a SyncEngine; returns an unsubscribe. Seeds from the current snapshot. */
  attach(engine: {
    subscribe: (listener: () => void) => () => void;
    getSnapshot: () => LadderObservable;
  }): () => void {
    this.observe(engine.getSnapshot());
    return engine.subscribe(() => this.observe(engine.getSnapshot()));
  }

  /** A point-in-time read of the metrics (the open rung's time is counted live). */
  getMetrics(): LadderMetrics {
    const msInRung = { ...this.msInRung };
    if (this.lastStage !== null) msInRung[this.lastStage] += this.now() - this.stageSince;
    const total =
      msInRung.full_video +
      msInRung.keyframe_ken_burns +
      msInRung.illustration +
      msInRung.audio_text_only;
    return {
      transitions: this.transitions,
      msInRung,
      stalls: this.stalls,
      lastSeekToFirstFrameMs: this.lastSeekToFirstFrameMs,
      maxSeekToFirstFrameMs: this.maxSeekToFirstFrameMs,
      fullVideoFraction: total > 0 ? msInRung.full_video / total : 0,
    };
  }
}
