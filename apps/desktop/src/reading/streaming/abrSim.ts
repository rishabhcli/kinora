// A pure trace-replay harness for the adaptive-quality controller. Given a
// timeline of {bandwidth, bufferAhead, decodeHealth, jank} samples, it drives the
// QualityController step-by-step and reports the kind of metrics §12.5 wants:
// average selected rung, number of quality switches (lower = less distracting),
// time spent at each rung, and "starvation" steps (buffer below the safe line
// while we were forced to downgrade). This validates the ladder's tuning offline
// and gives the observability panel a way to score a session's adaptation quality.
// No DOM, no timers — deterministic and fully unit-testable.

import {
  QualityController,
  type QualityConfig,
  type QualityLevel,
  DEFAULT_LADDER,
} from "./qualityLadder";

export interface TraceSample {
  /** ms timestamp of this sample (monotonic) */
  t: number;
  kbps: number;
  bufferAheadS?: number | null;
  decodeHealth?: "good" | "degraded" | "stalled";
  jankRatio?: number;
  saveData?: boolean;
}

export interface SimStep {
  t: number;
  index: number;
  levelId: string;
  reason: string;
  changed: boolean;
}

export interface SimResult {
  steps: SimStep[];
  /** number of rung changes across the trace */
  switches: number;
  /** mean selected rung index (lower = richer) */
  meanIndex: number;
  /** ms spent at each rung id */
  dwellMsByLevel: Record<string, number>;
  /** steps where the buffer was below `safeBufferS` while not at the bottom rung */
  starvedSteps: number;
  /** the richest (smallest) and leanest (largest) index visited */
  bestIndex: number;
  worstIndex: number;
}

export interface SimOptions {
  config?: QualityConfig;
  maxHeight?: number;
  startIndex?: number;
  /** the safe-buffer line used only for the starvation metric (defaults to config) */
  safeBufferS?: number;
}

/** Replay a bandwidth/buffer trace through the controller and score the result. */
export function simulateAbr(trace: TraceSample[], options: SimOptions = {}): SimResult {
  const ladder = options.config?.ladder ?? DEFAULT_LADDER;
  const controller = new QualityController(options.config ?? {}, options.startIndex ?? 1);
  const safeBuffer = options.safeBufferS ?? options.config?.safeBufferS ?? 4;

  const steps: SimStep[] = [];
  const dwellMsByLevel: Record<string, number> = {};
  let switches = 0;
  let sumIndex = 0;
  let starved = 0;
  let best = ladder.length - 1;
  let worst = 0;

  for (let i = 0; i < trace.length; i++) {
    const s = trace[i];
    const d = controller.update(
      {
        kbps: s.kbps,
        bufferAheadS: s.bufferAheadS ?? null,
        decodeHealth: s.decodeHealth,
        jankRatio: s.jankRatio,
        maxHeight: options.maxHeight,
        saveData: s.saveData,
        gpuAvailable: true,
      },
      s.t,
    );
    if (d.changed) switches++;
    sumIndex += d.index;
    if (d.index < best) best = d.index;
    if (d.index > worst) worst = d.index;

    // Dwell = time until the next sample (or 0 for the last).
    const dt = i + 1 < trace.length ? Math.max(0, trace[i + 1].t - s.t) : 0;
    dwellMsByLevel[d.level.id] = (dwellMsByLevel[d.level.id] ?? 0) + dt;

    const buffer = s.bufferAheadS ?? Infinity;
    const atBottom = d.index === ladder.length - 1;
    if (buffer < safeBuffer && !atBottom) starved++;

    steps.push({ t: s.t, index: d.index, levelId: d.level.id, reason: d.reason, changed: d.changed });
  }

  return {
    steps,
    switches,
    meanIndex: trace.length ? sumIndex / trace.length : 0,
    dwellMsByLevel,
    starvedSteps: starved,
    bestIndex: best,
    worstIndex: worst,
  };
}

/** Convenience: the rung a level id maps to (for asserting against DEFAULT_LADDER). */
export function levelById(id: string, ladder: readonly QualityLevel[] = DEFAULT_LADDER): QualityLevel | undefined {
  return ladder.find((l) => l.id === id);
}
