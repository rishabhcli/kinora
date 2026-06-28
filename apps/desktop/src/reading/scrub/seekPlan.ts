// Decides HOW to seek the active <video> for a given playhead, bridging the pure
// timeline math to FilmPane's imperative seek. Today FilmPane scrubs at ~30Hz with
// a raw time; this planner adds two refinements without changing that contract:
//
//   1. While actively SCRUBBING (mode === "scrub"), keep cheap continuous seeks —
//      smoothness matters more than landing on an exact frame mid-drag.
//   2. When SETTLING (mode flips scrub→play, or a paused dwell), QUANTISE the seek
//      to a frame centre (frameClock) so the resting frame is stable + repeatable
//      (no inter-frame shimmer between identical scroll positions).
//
// It also de-dupes: if the new target is the same frame as where the video already
// is, it returns `skip` so the engine avoids a redundant seek (seek thrash). Pure;
// the engine supplies the current time + fps + duration and applies the result.

import { quantizeTime, sameFrame, type FrameClockConfig } from "./frameClock";

export type SeekMode = "scrub" | "settle";

export interface SeekPlanInput {
  /** the desired time from the timeline (segmentTime output) */
  targetTimeS: number;
  /** the video's current time */
  currentTimeS: number;
  /** scrubbing (continuous) vs settling (quantise to a frame) */
  mode: SeekMode;
  /** the clip's fps + duration; fps ≤ 0 means unknown → never quantise */
  clock: FrameClockConfig;
  /** don't re-seek within this many seconds while scrubbing (≈ 1 frame); default 1/30 */
  epsilonS?: number;
}

export interface SeekPlan {
  /** issue a seek to this time, or skip when null */
  seekToS: number | null;
  /** did we quantise to a frame centre? */
  quantized: boolean;
  /** the frame index we're landing on (for telemetry; -1 when continuous/unknown) */
  frameIndex: number;
}

const DEFAULT_EPSILON_S = 1 / 30;

const SKIP: SeekPlan = { seekToS: null, quantized: false, frameIndex: -1 };

/** Plan the seek. Continuous (epsilon-gated) while scrubbing; frame-quantised +
 *  same-frame-deduped while settling. */
export function planSeek(input: SeekPlanInput): SeekPlan {
  const { targetTimeS, currentTimeS, mode, clock } = input;
  const eps = input.epsilonS ?? DEFAULT_EPSILON_S;
  if (!Number.isFinite(targetTimeS)) return SKIP;

  if (mode === "scrub") {
    // Continuous: only seek when we've moved more than ~a frame, to avoid thrash.
    if (Math.abs(targetTimeS - currentTimeS) <= eps) return SKIP;
    return { seekToS: targetTimeS, quantized: false, frameIndex: -1 };
  }

  // Settling: quantise to a frame centre for a stable resting frame.
  if (!(clock.fps > 0)) {
    // Unknown fps → can't quantise; fall back to a plain (epsilon-gated) seek.
    if (Math.abs(targetTimeS - currentTimeS) <= eps) return SKIP;
    return { seekToS: targetTimeS, quantized: false, frameIndex: -1 };
  }
  // If we're already on the target frame, skip (repeatable rest, no re-seek).
  if (sameFrame(clock, targetTimeS, currentTimeS)) return SKIP;
  const q = quantizeTime(clock, targetTimeS);
  return { seekToS: q.timeS, quantized: true, frameIndex: q.index };
}
