import type { SyncEngine } from "@kinora/core";
import { useEffect, useState, useSyncExternalStore } from "react";

const DEFAULT_IDLE_MS = 8000;

/**
 * True when the reading room is idle and the ladder's perpetual motion should
 * rest — the §4.7 idle-pause applied client-side to the Ken-Burns loop (and any
 * playing media): the tab is hidden, or there has been no playhead activity
 * (scroll / playback / events) for `idleMs`. It resumes the instant the tab is
 * shown again or the reader moves, so resumption is free. Driven off the engine's
 * own change stream, so "activity" needs no extra plumbing.
 */
export function useIdlePause(engine: SyncEngine, idleMs = DEFAULT_IDLE_MS): boolean {
  const [hidden, setHidden] = useState(() =>
    typeof document !== "undefined" ? document.hidden : false,
  );
  const [idle, setIdle] = useState(false);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const onVis = (): void => setHidden(document.hidden);
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const arm = (): void => {
      clearTimeout(timer);
      setIdle(false);
      timer = setTimeout(() => setIdle(true), idleMs);
    };
    arm(); // every engine emit (scroll, playback, events) counts as activity
    const unsub = engine.subscribe(arm);
    return () => {
      clearTimeout(timer);
      unsub();
    };
  }, [engine, idleMs]);

  // Never go idle mid-playback (a long silent clip emits few changes); a hidden
  // tab always pauses, playing or not.
  const isPlaying = useSyncExternalStore(engine.subscribe, () => engine.getSnapshot().isPlaying);
  return hidden || (idle && !isPlaying);
}
