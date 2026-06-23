import type { SyncEngine } from "@kinora/core";
import { useEffect, useState, useSyncExternalStore } from "react";
import { AppState } from "react-native";

const DEFAULT_IDLE_MS = 8000;

/**
 * True when the reading room is idle and the ladder's perpetual motion should
 * rest — the §4.7 idle-pause applied client-side to the Ken-Burns loop (and the
 * player): the app is backgrounded, or there has been no playhead activity
 * (scroll / playback / events) for `idleMs`. Resumes the instant the app is
 * foregrounded or the reader moves. Driven off the engine's own change stream.
 */
export function useIdlePause(engine: SyncEngine, idleMs = DEFAULT_IDLE_MS): boolean {
  const [active, setActive] = useState(AppState.currentState === "active");
  const [idle, setIdle] = useState(false);

  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => setActive(state === "active"));
    return () => sub.remove();
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const arm = (): void => {
      clearTimeout(timer);
      setIdle(false);
      timer = setTimeout(() => setIdle(true), idleMs);
    };
    arm();
    const unsub = engine.subscribe(arm);
    return () => {
      clearTimeout(timer);
      unsub();
    };
  }, [engine, idleMs]);

  const isPlaying = useSyncExternalStore(engine.subscribe, () => engine.getSnapshot().isPlaying);
  return !active || (idle && !isPlaying);
}
