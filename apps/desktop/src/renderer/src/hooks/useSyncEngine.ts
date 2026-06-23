import {
  SessionSocket,
  SyncEngine,
  type SyncSnapshot,
  type WebSocketLike,
} from "@kinora/core";
import { useEffect, useMemo, useSyncExternalStore } from "react";

import { api } from "../lib/api";
import { authStore } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

export interface UseSyncEngineResult {
  engine: SyncEngine;
  snapshot: SyncSnapshot;
}

/**
 * Own the per-session SyncEngine: bind it into React via useSyncExternalStore,
 * push debounced intent + seeks to the Scheduler, and open the live socket so
 * `clip_ready` events hot-swap rendered clips into the playhead.
 */
export function useSyncEngine(sessionId: string | null): UseSyncEngineResult {
  const engine = useMemo(
    () =>
      new SyncEngine({
        callbacks: {
          onIntent: (intent) => {
            if (!sessionId) return;
            void api.POST("/api/sessions/{session_id}/intent", {
              params: { path: { session_id: sessionId } },
              body: { focus_word: intent.focusWord, velocity: intent.velocity, mode: intent.mode },
            });
          },
          onSeek: (word) => {
            if (!sessionId) return;
            void api.POST("/api/sessions/{session_id}/seek", {
              params: { path: { session_id: sessionId } },
              body: { word },
            });
          },
        },
      }),
    [sessionId],
  );

  const snapshot = useSyncExternalStore(engine.subscribe, engine.getSnapshot);

  useEffect(() => {
    if (!sessionId) return;
    const socket = new SessionSocket({
      baseUrl: API_BASE_URL,
      sessionId,
      getToken: () => authStore.getState().token,
      createWebSocket: (url) => new WebSocket(url) as unknown as WebSocketLike,
      onEvent: (event) => {
        if (event.event === "clip_ready" && event.sync_segment) {
          engine.ingestClip(event.sync_segment, event.oss_url ?? undefined);
        }
      },
    });
    void socket.connect();
    return () => socket.close();
  }, [sessionId, engine]);

  return { engine, snapshot };
}
