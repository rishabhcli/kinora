/**
 * Buffer-surfacing logic for the §5.3 indicator — framework-agnostic and pure, so
 * the desktop (Electron) and mobile (Expo) shells share one implementation and it
 * is exhaustively unit-testable.
 *
 * The buffer hairline fills toward the high watermark `H`; its occupancy is either
 * the live committed-seconds-ahead (when real video is generating) or a playback
 * cursor over the recomputed §4.10 buffer-trace sawtooth (the zero-video proof,
 * the default with the live gate off). The zone badge names the representation
 * ahead, and a stall surface flags the §4.11 "reader outran the render" case.
 */
import type { BufferPoint } from "../eval/report";
import type { BufferZone } from "../events";
import type { BeatStage } from "./SyncEngine";

/** Sim-seconds of the recomputed sawtooth advanced per real second while reading. */
export const SAWTOOTH_PLAY_RATE = 6;
/** A reader counts as "active" (buffer playing) within this long of a focus move (§4.7). */
export const BUFFER_ACTIVE_WINDOW_MS = 2200;
/** A committed buffer below this many seconds, with renders in flight, has stalled. */
export const STALL_SECONDS = 1.5;
/** Below this fraction of H the displayed buffer reads as "planning ahead" (cold). */
export const COLD_FRACTION = 0.08;

/** Human labels for the three §4.4 zones (badge text). */
export const ZONE_LABEL: Record<BufferZone, string> = {
  committed: "Full film",
  speculative: "Preview still",
  cold: "Planning ahead",
};

/** The badge label when the buffer has drained while the reader pushes on (§4.11). */
export const STALL_LABEL = "Catching up";

/** The quiet budget-low notice, tied to the actual §12.4 ladder rung on screen. */
export const STAGE_NOTICE: Record<BeatStage, string> = {
  full_video: "Easing film budget",
  keyframe_ken_burns: "Budget low — Ken-Burns over the keyframe",
  illustration: "Budget low — panning the book’s own art",
  audio_text_only: "Budget low — narrating with the page",
};

/** Occupancy seconds as a clamped fraction of the high watermark (0..1). */
export function bufferFraction(occupancyS: number, highS: number): number {
  if (!(highS > 0)) return 0;
  return Math.max(0, Math.min(1, occupancyS / highS));
}

/** Linearly interpolate committed-seconds-ahead at reading-time `t` on the trace. */
export function sampleSawtoothAt(trace: readonly BufferPoint[], t: number): number {
  const first = trace[0];
  const last = trace[trace.length - 1];
  if (!first || !last) return 0;
  if (t <= first.t) return first.committed_seconds_ahead;
  if (t >= last.t) return last.committed_seconds_ahead;
  for (let i = 1; i < trace.length; i += 1) {
    const a = trace[i - 1];
    const b = trace[i];
    if (!a || !b) continue;
    if (b.t >= t) {
      const span = b.t - a.t || 1;
      const f = (t - a.t) / span;
      return a.committed_seconds_ahead + f * (b.committed_seconds_ahead - a.committed_seconds_ahead);
    }
  }
  return last.committed_seconds_ahead;
}

/** Advance the playback cursor by `dtS` real seconds, looping over the trace length. */
export function advanceSawtoothCursor(
  cursorS: number,
  dtS: number,
  tMaxS: number,
  rate: number = SAWTOOTH_PLAY_RATE,
): number {
  if (!(tMaxS > 0)) return 0;
  let next = cursorS + dtS * rate;
  while (next > tMaxS) next -= tMaxS;
  return next < 0 ? 0 : next;
}

/** Whether the reader moved within the active window (the buffer plays vs holds). */
export function isReaderActive(
  lastMoveMs: number,
  nowMs: number,
  windowMs: number = BUFFER_ACTIVE_WINDOW_MS,
): boolean {
  return nowMs - lastMoveMs < windowMs;
}

export interface ZoneInput {
  /** Authoritative zone from the live buffer_state event, if present (§4.6). */
  authoritativeZone?: BufferZone | null;
  /** The §12.4 ladder rung currently on the cinema stage. */
  stage: BeatStage;
  budgetLow: boolean;
  /** Displayed occupancy as a fraction of H (0..1). */
  fraction: number;
}

/**
 * The viewer zone — the backend's authoritative classification when present (it
 * mirrors the §4.6 promotion decision, so it is meaningful even with the live gate
 * off), otherwise a faithful client estimate from the live ladder rung + budget +
 * occupancy. Skim and budget pressure ride the keyframe ladder (speculative).
 */
export function deriveZone(input: ZoneInput): BufferZone {
  if (input.authoritativeZone) return input.authoritativeZone;
  if (input.budgetLow) return "speculative";
  if (input.stage === "full_video") return "committed";
  if (input.fraction < COLD_FRACTION) return "cold";
  return "speculative";
}

export interface BufferSurfaceInput extends ZoneInput {
  /** Whether the reader is actively reading (the buffer is playing, not idle). */
  active: boolean;
  /** Live committed-seconds-ahead from the event (null when there's no live signal). */
  liveCommittedAheadS?: number | null;
  /** In-flight committed (full-video) renders, from the enriched event. */
  inflightCommitted?: number;
}

export interface BufferSurface {
  zone: BufferZone;
  /** Badge text — the zone label, or "Catching up" while stalled. */
  label: string;
  /**
   * True when the reader has outrun the render: a near-empty *live* committed
   * buffer on a degraded rung while committed renders are in flight (§4.11). It
   * is deliberately gated on `inflightCommitted > 0`, so the live-gate-off build
   * (which never promotes) never false-flags a stall — keyframe-by-design is not
   * a stall.
   */
  stalled: boolean;
}

/** Classify the whole §5.3 surface (zone + label + stall) from one input. */
export function classifyBufferSurface(input: BufferSurfaceInput): BufferSurface {
  const zone = deriveZone(input);
  const live = input.liveCommittedAheadS;
  const stalled =
    input.active &&
    input.stage !== "full_video" &&
    (input.inflightCommitted ?? 0) > 0 &&
    live != null &&
    live < STALL_SECONDS;
  return { zone, label: stalled ? STALL_LABEL : ZONE_LABEL[zone], stalled };
}
