import { clamp } from "./math";

// The watermark buffer (kinora.md §4.5). The committed buffer is measured in
// *video-seconds ahead of the focus playhead*. The UI surfaces it as a faint
// hairline that fills toward H — the only visible sign of the generation
// machinery (§5.3).
export const LOW_WATERMARK_S = 25;
export const HIGH_WATERMARK_S = 75;
export const COMMIT_HORIZON_S = 45;

/** Fill fraction (0..1) for the buffer hairline, filling toward the high mark. */
export function bufferFillFraction(
  committedSecondsAhead: number,
  high: number = HIGH_WATERMARK_S,
): number {
  if (high <= 0) return 0;
  return clamp(committedSecondsAhead / high, 0, 1);
}

/** Where the low watermark sits as a fraction of the hairline's length. */
export function lowMarkFraction(
  low: number = LOW_WATERMARK_S,
  high: number = HIGH_WATERMARK_S,
): number {
  if (high <= 0) return 0;
  return clamp(low / high, 0, 1);
}

export type BufferHealth = "low" | "ok" | "full";

/**
 * Classify the buffer against the watermarks. Below L the scheduler bursts to
 * refill; at/above H it idles (the hysteresis band that makes generation
 * "smooth *and* not-always-generating").
 */
export function bufferHealth(
  committedSecondsAhead: number,
  low: number = LOW_WATERMARK_S,
  high: number = HIGH_WATERMARK_S,
): BufferHealth {
  if (committedSecondsAhead < low) return "low";
  if (committedSecondsAhead >= high) return "full";
  return "ok";
}
