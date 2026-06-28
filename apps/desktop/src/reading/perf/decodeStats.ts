// Video decode / playback health, the pure half. The HTMLVideoElement exposes
// `getVideoPlaybackQuality()` (totalVideoFrames / droppedVideoFrames /
// corruptedVideoFrames) and the experimental `webkitDecodedFrameCount`. Those are
// *cumulative* counters; this module turns successive readings into rates and a
// health verdict, with no DOM dependency (feed it the raw counters). The decode
// drop rate is a separate axis from the rAF frame budget: the compositor can hit
// 60fps while the *decoder* is dropping frames under a heavy clip — and that is
// exactly the signal the adaptive-quality controller wants.

/** A cumulative reading off `HTMLVideoElement.getVideoPlaybackQuality()` (plus
 *  the element's `currentTime` so we can derive a presented-fps). */
export interface PlaybackQualityReading {
  /** total frames the decoder has created for presentation (cumulative) */
  totalVideoFrames: number;
  /** frames dropped before presentation (cumulative) */
  droppedVideoFrames: number;
  /** frames detected as corrupted (cumulative) */
  corruptedVideoFrames?: number;
  /** wall-clock timestamp of the reading (ms) — `performance.now()` upstream */
  atMs: number;
}

export interface DecodeDelta {
  /** frames presented since the previous reading */
  presented: number;
  /** frames dropped since the previous reading */
  dropped: number;
  /** frames corrupted since the previous reading */
  corrupted: number;
  /** ms elapsed since the previous reading */
  elapsedMs: number;
  /** dropped / (presented + dropped) over the interval, [0,1] */
  dropRate: number;
  /** presented frames per second over the interval */
  presentedFps: number;
}

export type DecodeHealth = "good" | "degraded" | "stalled";

export interface DecodeStatsConfig {
  /** dropRate above this → "degraded" (default 0.05 = 5%) */
  degradedDropRate?: number;
  /** dropRate above this → "stalled" (default 0.2 = 20%) */
  stalledDropRate?: number;
  /** presentedFps below this (with elapsed time) also signals a stall (default 5) */
  stallFps?: number;
}

const DEFAULT_DEGRADED = 0.05;
const DEFAULT_STALLED = 0.2;
const DEFAULT_STALL_FPS = 5;

/** Computes per-interval decode deltas from cumulative readings. Resets cleanly
 *  when the counters go backwards (a new <video> element / source swap restarts
 *  the cumulative counts at 0) so a source change never reports a giant negative. */
export class DecodeStats {
  private prev: PlaybackQualityReading | null = null;
  private readonly degraded: number;
  private readonly stalled: number;
  private readonly stallFps: number;
  private lastDelta: DecodeDelta | null = null;

  constructor(config: DecodeStatsConfig = {}) {
    this.degraded = config.degradedDropRate ?? DEFAULT_DEGRADED;
    this.stalled = config.stalledDropRate ?? DEFAULT_STALLED;
    this.stallFps = config.stallFps ?? DEFAULT_STALL_FPS;
  }

  /** Feed a fresh cumulative reading; returns the interval delta, or null on the
   *  first reading / after a counter reset (no prior baseline to diff against). */
  push(reading: PlaybackQualityReading): DecodeDelta | null {
    const prev = this.prev;
    this.prev = reading;
    if (!prev) return null;
    // Counter reset (source swap) → re-baseline, emit nothing for this interval.
    if (
      reading.totalVideoFrames < prev.totalVideoFrames ||
      reading.droppedVideoFrames < prev.droppedVideoFrames
    ) {
      this.lastDelta = null;
      return null;
    }
    const droppedTotalDelta = reading.droppedVideoFrames - prev.droppedVideoFrames;
    const totalDelta = reading.totalVideoFrames - prev.totalVideoFrames;
    // `totalVideoFrames` already counts dropped frames; presented = total − dropped.
    const presented = Math.max(0, totalDelta - droppedTotalDelta);
    const dropped = Math.max(0, droppedTotalDelta);
    const corrupted = Math.max(0, (reading.corruptedVideoFrames ?? 0) - (prev.corruptedVideoFrames ?? 0));
    const elapsedMs = Math.max(0, reading.atMs - prev.atMs);
    const denom = presented + dropped;
    const delta: DecodeDelta = {
      presented,
      dropped,
      corrupted,
      elapsedMs,
      dropRate: denom > 0 ? dropped / denom : 0,
      presentedFps: elapsedMs > 0 ? (presented / elapsedMs) * 1000 : 0,
    };
    this.lastDelta = delta;
    return delta;
  }

  /** The most recent interval delta, or null. */
  get latest(): DecodeDelta | null {
    return this.lastDelta;
  }

  /** Classify the latest interval. Defaults to "good" before any data. */
  health(): DecodeHealth {
    return classifyDecode(this.lastDelta, {
      degradedDropRate: this.degraded,
      stalledDropRate: this.stalled,
      stallFps: this.stallFps,
    });
  }

  /** Forget the baseline (source swap handled explicitly, or book change). */
  reset(): void {
    this.prev = null;
    this.lastDelta = null;
  }
}

/** Pure classifier so the controller and tests can grade a delta directly. */
export function classifyDecode(delta: DecodeDelta | null, config: DecodeStatsConfig = {}): DecodeHealth {
  if (!delta) return "good";
  const degraded = config.degradedDropRate ?? DEFAULT_DEGRADED;
  const stalled = config.stalledDropRate ?? DEFAULT_STALLED;
  const stallFps = config.stallFps ?? DEFAULT_STALL_FPS;
  if (delta.dropRate >= stalled) return "stalled";
  // A near-zero presented-fps over a real interval, while something was supposed
  // to be playing, is a stall even if the drop counter hasn't moved.
  if (delta.elapsedMs >= 250 && delta.presented === 0 && delta.dropped > 0) return "stalled";
  if (delta.dropRate >= degraded || (delta.elapsedMs >= 500 && delta.presentedFps < stallFps && delta.presented > 0)) {
    return "degraded";
  }
  return "good";
}
