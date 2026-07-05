// The §12.5 observability snapshot, pure half. The metrics panel wants a single,
// honest readout of how the reading room is performing:
// render smoothness, decode health, the adaptive rung and why, and the buffer
// occupancy. This module FUSES the independent cores — FrameStats, DecodeStats,
// the quality decision, and the SSE buffer signal — into one flat, serialisable
// snapshot plus a coarse health grade, with no DOM. The DOM panel just renders
// what this returns; the engine builds it from the live signals each tick.

import type { FrameStatsSnapshot } from "./frameStats";
import type { DecodeHealth } from "./decodeStats";

export interface BufferSignal {
  /** committed seconds buffered ahead (SSE buffer_state); null = unknown/off */
  committedAheadS: number | null;
  /** is the scheduler bursting to refill? */
  bursting: boolean;
  /** in-flight render counts, if known */
  inflightCommitted?: number;
  inflightSpeculative?: number;
  /** the scheduler zone label, if known */
  zone?: string | null;
}

export interface QualitySignal {
  levelId: string;
  levelLabel: string;
  tier: string;
  reason: string;
}

export interface ObservabilityInput {
  frame: FrameStatsSnapshot;
  decodeHealth: DecodeHealth;
  decodeDropRate: number;
  quality?: QualitySignal;
  buffer?: BufferSignal;
  /** estimated link speed (kbps), if known */
  kbps?: number;
  /** is the GPU compositor currently drawing? */
  gpuActive?: boolean;
}

export type HealthGrade = "smooth" | "minor-hitches" | "struggling";

export interface ObservabilitySnapshot {
  /** rounded headline numbers for the panel */
  fps: number;
  p95Ms: number;
  jankPct: number;
  droppedFrames: number;
  decodeHealth: DecodeHealth;
  decodeDropPct: number;
  rung: string | null;
  rungReason: string | null;
  kbps: number | null;
  committedAheadS: number | null;
  bursting: boolean;
  zone: string | null;
  gpuActive: boolean;
  /** a single coarse grade combining frame + decode health */
  grade: HealthGrade;
}

const round = (v: number, dp = 0): number => {
  const m = Math.pow(10, dp);
  return Math.round(v * m) / m;
};

/** Combine the cores into one panel-ready snapshot. */
export function buildObservability(input: ObservabilityInput): ObservabilitySnapshot {
  const { frame } = input;
  return {
    fps: round(frame.fps, 1),
    p95Ms: round(frame.p95Ms, 1),
    jankPct: round(frame.jankRatio * 100, 1),
    droppedFrames: frame.droppedFrames,
    decodeHealth: input.decodeHealth,
    decodeDropPct: round(input.decodeDropRate * 100, 1),
    rung: input.quality?.levelLabel ?? null,
    rungReason: input.quality?.reason ?? null,
    kbps: input.kbps != null ? round(input.kbps) : null,
    committedAheadS: input.buffer?.committedAheadS ?? null,
    bursting: input.buffer?.bursting ?? false,
    zone: input.buffer?.zone ?? null,
    gpuActive: input.gpuActive ?? false,
    grade: gradeHealth(input),
  };
}

/** Coarse health: the worse of frame smoothness and decode health drives it, so
 *  the panel's single dot is honest about the worst axis. */
export function gradeHealth(input: ObservabilityInput): HealthGrade {
  const { frame } = input;
  const decode = input.decodeHealth;
  // Decode dominates: a stalled decoder is "struggling" regardless of rAF fps.
  if (decode === "stalled") return "struggling";
  if (frame.count > 0 && frame.jankRatio >= 0.2) return "struggling";
  if (decode === "degraded") return "minor-hitches";
  if (frame.count > 0 && (frame.jankRatio >= 0.05 || frame.fps < 50)) return "minor-hitches";
  return "smooth";
}
