import { useSyncExternalStore } from "react";

import type { SyncEngine, SyncSnapshot } from "./SyncEngine";

/** Subscribe a React component to the SyncEngine's snapshot. */
export function useSyncSnapshot(engine: SyncEngine): SyncSnapshot {
  return useSyncExternalStore(engine.subscribe, engine.getSnapshot, engine.getSnapshot);
}
