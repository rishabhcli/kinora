// The DOM adapter that turns the pure perf cores into a live signal for the
// reading room. It runs a lightweight rAF loop that feeds frame *durations* into
// a FrameStats ring, polls the active <video>'s getVideoPlaybackQuality() into a
// DecodeStats at a low cadence, and publishes a throttled snapshot through
// useSyncExternalStore so the observability panel (and the adaptive-quality
// controller) can read current fps / jank / decode-health without re-rendering
// the film every frame.
//
// Pure logic lives in frameStats.ts / decodeStats.ts; this file is the thin,
// inevitably-DOM half (performance.now, requestAnimationFrame, the <video>
// element). It is inert (records nothing) when `enabled` is false so it costs
// nothing when the panel is closed.
import { useEffect, useMemo, useRef, useSyncExternalStore, type RefObject } from "react";
import { FrameStats, type FrameStatsConfig, type FrameStatsSnapshot } from "./frameStats";
import { DecodeStats, type DecodeHealth } from "./decodeStats";

/** A <video> we can read decode quality from. Kept structural so tests / non-DOM
 *  callers can supply a stub. */
export interface DecodeProbe {
  getVideoPlaybackQuality?: () => {
    totalVideoFrames: number;
    droppedVideoFrames: number;
    corruptedVideoFrames?: number;
  };
}

export interface PerfSnapshot extends FrameStatsSnapshot {
  decodeHealth: DecodeHealth;
  /** decoder drop rate over the last interval [0,1] */
  decodeDropRate: number;
}

export interface UsePerfMonitorOptions extends FrameStatsConfig {
  /** record frames only while true (panel open / debug build) */
  enabled?: boolean;
  /** how often to publish a snapshot + poll decode stats (ms); default 500 */
  publishIntervalMs?: number;
  /** the active film <video> to read decode quality from (optional) */
  videoRef?: RefObject<DecodeProbe | null>;
}

const DEFAULT_PUBLISH_MS = 500;

const EMPTY: PerfSnapshot = {
  count: 0,
  fps: 0,
  meanMs: 0,
  p95Ms: 0,
  maxMs: 0,
  overBudgetRatio: 0,
  jankCount: 0,
  jankRatio: 0,
  droppedFrames: 0,
  lifetime: { frames: 0, jank: 0, dropped: 0 },
  decodeHealth: "good",
  decodeDropRate: 0,
};

/** A tiny external store so consumers re-render on snapshot publish, not per frame. */
class PerfStore {
  private snap: PerfSnapshot = EMPTY;
  private listeners = new Set<() => void>();
  get(): PerfSnapshot {
    return this.snap;
  }
  set(next: PerfSnapshot): void {
    this.snap = next;
    for (const l of this.listeners) l();
  }
  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }
}

export interface PerfMonitor {
  /** the latest published snapshot (re-renders the consumer on publish) */
  snapshot: PerfSnapshot;
  /** imperative read for hot-path consumers that must not re-render */
  read(): PerfSnapshot;
}

export function usePerfMonitor(options: UsePerfMonitorOptions = {}): PerfMonitor {
  const { enabled = true, publishIntervalMs = DEFAULT_PUBLISH_MS, videoRef } = options;
  const store = useMemo(() => new PerfStore(), []);
  const stats = useMemo(
    () => new FrameStats({ budgetMs: options.budgetMs, jankFactor: options.jankFactor, windowSize: options.windowSize }),
    // FrameStats is config-stable for the hook's life; book-change resets via .reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const decode = useMemo(() => new DecodeStats(), []);
  const rafRef = useRef(0);
  const prevTs = useRef(0);
  const seeded = useRef(false);
  const lastPublish = useRef(0);

  useEffect(() => {
    if (!enabled) {
      store.set(EMPTY);
      stats.reset();
      decode.reset();
      return;
    }
    prevTs.current = 0;
    seeded.current = false;
    lastPublish.current = 0;
    const now = () => (typeof performance !== "undefined" ? performance.now() : Date.now());

    const tick = (ts: number) => {
      // Seed on the very first frame (don't record a duration against an unknown
      // prior timestamp); record real durations from the second frame on. Handles
      // a first rAF timestamp of exactly 0 (as in tests / a freshly-loaded page).
      if (seeded.current) stats.record(ts - prevTs.current);
      else {
        seeded.current = true;
        lastPublish.current = ts;
      }
      prevTs.current = ts;

      if (ts - lastPublish.current >= publishIntervalMs) {
        lastPublish.current = ts;
        // Poll decode quality (cumulative → interval delta).
        const probe = videoRef?.current;
        const q = probe?.getVideoPlaybackQuality?.();
        if (q) {
          decode.push({
            totalVideoFrames: q.totalVideoFrames,
            droppedVideoFrames: q.droppedVideoFrames,
            corruptedVideoFrames: q.corruptedVideoFrames,
            atMs: now(),
          });
        }
        const base = stats.snapshot();
        const latest = decode.latest;
        store.set({
          ...base,
          decodeHealth: decode.health(),
          decodeDropRate: latest?.dropRate ?? 0,
        });
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(rafRef.current);
    };
  }, [enabled, publishIntervalMs, store, stats, decode, videoRef]);

  const snapshot = useSyncExternalStore(
    (cb) => store.subscribe(cb),
    () => store.get(),
    () => EMPTY,
  );

  return { snapshot, read: () => store.get() };
}
