// The DOM adapter for adaptive multi-quality streaming. It owns a
// BandwidthEstimator + a QualityController and, on a low-cadence tick, folds the
// live signals — link speed (from instrumented clip fetches), committed buffer
// ahead (the SSE buffer_state the shell already receives), decoder health and rAF
// jank (perf monitor), and the device's pixel ceiling — into the chosen quality
// rung. The chosen rung is published through useSyncExternalStore so the engine
// can resolve a variant URL / annotate the scheduler, and the observability panel
// can show why the film is at the fidelity it's at.
//
// Pure logic lives in bandwidth.ts / qualityLadder.ts; this is the thin React +
// timer half. With KINORA_LIVE_VIDEO OFF there is one fallback film and no buffer
// signal — the controller simply settles on a steady rung and never blanks the
// pane (the bottom rung is always valid).
import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { BandwidthEstimator } from "./bandwidth";
import {
  QualityController,
  type QualityConfig,
  type QualityDecision,
  type QualityInputs,
  type QualityLevel,
  DEFAULT_LADDER,
} from "./qualityLadder";

export interface AdaptiveSignals {
  /** committed seconds buffered ahead (SSE buffer_state); null/undefined = unknown */
  bufferAheadS?: number | null;
  /** decoder health from usePerfMonitor */
  decodeHealth?: "good" | "degraded" | "stalled";
  /** rolling rAF jank ratio from usePerfMonitor */
  jankRatio?: number;
  /** honour data-saver / "simple film" intent */
  saveData?: boolean;
}

export interface UseAdaptiveQualityOptions {
  /** the bandwidth estimator to read (share the one fed by instrumented fetches) */
  estimator: BandwidthEstimator;
  /** a live getter for the fused signals (read each tick; avoids stale closures) */
  getSignals: () => AdaptiveSignals;
  /** decision cadence in ms (default 2000) */
  intervalMs?: number;
  /** controller tuning */
  config?: QualityConfig;
  /** device pixel ceiling for the pane (height px × dpr); default unbounded */
  maxHeight?: number;
  /** run the controller only while true (default true) */
  enabled?: boolean;
}

class QualityStore {
  private decision: QualityDecision;
  private listeners = new Set<() => void>();
  constructor(level: QualityLevel, index: number) {
    this.decision = { level, index, reason: "init", changed: false };
  }
  get(): QualityDecision {
    return this.decision;
  }
  set(d: QualityDecision): void {
    // Only notify on an actual rung change to avoid churn.
    if (d.index !== this.decision.index || d.reason !== this.decision.reason) {
      this.decision = d;
      for (const l of this.listeners) l();
    }
  }
  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }
}

export interface AdaptiveQuality {
  /** the current decision (re-renders the consumer when the rung changes) */
  decision: QualityDecision;
  /** imperative read for hot-path callers */
  read(): QualityDecision;
}

export function useAdaptiveQuality(options: UseAdaptiveQualityOptions): AdaptiveQuality {
  const { estimator, getSignals, intervalMs = 2000, config, maxHeight, enabled = true } = options;
  const ladder = config?.ladder ?? DEFAULT_LADDER;
  const controller = useMemo(() => new QualityController(config ?? {}), [config]);
  const store = useMemo(() => new QualityStore(controller.current(), controller.currentIndex()), [controller]);
  const getRef = useRef(getSignals);
  getRef.current = getSignals;

  useEffect(() => {
    if (!enabled) return;
    const now = () => (typeof performance !== "undefined" ? performance.now() : Date.now());
    const decide = () => {
      const s = getRef.current();
      const inputs: QualityInputs = {
        kbps: estimator.kbps(),
        bufferAheadS: s.bufferAheadS ?? null,
        decodeHealth: s.decodeHealth,
        jankRatio: s.jankRatio,
        maxHeight,
        saveData: s.saveData,
        gpuAvailable: true,
      };
      store.set(controller.update(inputs, now()));
    };
    decide(); // settle immediately
    const id = setInterval(decide, intervalMs);
    return () => clearInterval(id);
  }, [enabled, estimator, intervalMs, controller, store, maxHeight]);

  const decision = useSyncExternalStore(
    (cb) => store.subscribe(cb),
    () => store.get(),
    () => ({ level: ladder[controller.currentIndex()], index: controller.currentIndex(), reason: "ssr", changed: false }),
  );

  return { decision, read: () => store.get() };
}
