// Bandwidth estimation, pure half. The adaptive-quality controller needs a
// throughput estimate to pick a clip variant it can actually download ahead of
// the reader. We measure it the way an ABR player does: from completed clip
// fetches (bytes / seconds), smoothed with an exponentially-weighted moving
// average so one slow request doesn't yank the estimate, biased CONSERVATIVE
// (a downward sample moves the estimate faster than an upward one) so we never
// optimistically pick a tier we can't sustain.
//
// DOM-free: the ClipCache (or a fetch wrapper) reports {bytes, durationMs} when a
// download completes; this turns the stream of samples into a kbps estimate.

export interface ThroughputSample {
  /** bytes transferred */
  bytes: number;
  /** wall-clock download duration in ms */
  durationMs: number;
}

export interface BandwidthConfig {
  /** EWMA weight for an *upward* sample (slower to trust improvements). 0..1 */
  upAlpha?: number;
  /** EWMA weight for a *downward* sample (quick to react to congestion). 0..1 */
  downAlpha?: number;
  /** ignore samples smaller than this many bytes (too noisy to be meaningful) */
  minBytes?: number;
  /** seed estimate in kbps before any sample (default 6000 = 6 Mbps) */
  initialKbps?: number;
}

const DEFAULT_UP_ALPHA = 0.25;
const DEFAULT_DOWN_ALPHA = 0.55;
const DEFAULT_MIN_BYTES = 8 * 1024; // 8 KiB
const DEFAULT_INITIAL_KBPS = 6000;

/** Asymmetric EWMA bandwidth estimator in kbps. */
export class BandwidthEstimator {
  private estimate: number;
  private readonly upAlpha: number;
  private readonly downAlpha: number;
  private readonly minBytes: number;
  private samples = 0;

  constructor(config: BandwidthConfig = {}) {
    this.upAlpha = clamp01(config.upAlpha ?? DEFAULT_UP_ALPHA);
    this.downAlpha = clamp01(config.downAlpha ?? DEFAULT_DOWN_ALPHA);
    this.minBytes = config.minBytes ?? DEFAULT_MIN_BYTES;
    this.estimate = config.initialKbps && config.initialKbps > 0 ? config.initialKbps : DEFAULT_INITIAL_KBPS;
  }

  /** Fold one completed transfer into the estimate. Returns the new estimate
   *  (kbps), or the unchanged estimate for a sample too small/short to trust. */
  addSample(sample: ThroughputSample): number {
    if (sample.bytes < this.minBytes || sample.durationMs <= 0 || !Number.isFinite(sample.durationMs)) {
      return this.estimate;
    }
    const instantKbps = (sample.bytes * 8) / sample.durationMs; // bytes→bits, ms→s cancels (×1000/1000)
    if (!Number.isFinite(instantKbps) || instantKbps <= 0) return this.estimate;
    const alpha = instantKbps < this.estimate ? this.downAlpha : this.upAlpha;
    this.estimate = this.estimate + alpha * (instantKbps - this.estimate);
    this.samples++;
    return this.estimate;
  }

  /** Current estimate in kbps. */
  kbps(): number {
    return this.estimate;
  }

  /** Current estimate in Mbps. */
  mbps(): number {
    return this.estimate / 1000;
  }

  /** Number of trusted samples folded in (0 ⇒ the estimate is still the seed). */
  get sampleCount(): number {
    return this.samples;
  }

  /** Re-seed the estimate (book change / network type change). */
  reset(initialKbps = DEFAULT_INITIAL_KBPS): void {
    this.estimate = initialKbps > 0 ? initialKbps : DEFAULT_INITIAL_KBPS;
    this.samples = 0;
  }
}

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}
