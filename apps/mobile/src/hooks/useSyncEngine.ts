import {
  activityFromEvent,
  type components,
  type KinoraEvent,
  MAX_SESSION_ACTIVITY,
  type SessionActivity,
  SessionSocket,
  type SocketStatus,
  SyncEngine,
  type SyncSnapshot,
  type WebSocketLike,
} from "@kinora/core";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";

import { api } from "../lib/api";
import { authStore } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

export interface UseSyncEngineResult {
  engine: SyncEngine;
  snapshot: SyncSnapshot;
  /** The §5.4 live agent-activity feed, newest first (capped). */
  activity: SessionActivity[];
  /** Live socket state for the feed's link indicator. */
  socketStatus: SocketStatus;
  /** Record the Director's resolution of a surfaced conflict (§7.2). */
  resolveConflict: (conflictId: string, option: string) => void;
}

/** Per-session SyncEngine for mobile: binds into React, pushes intent/seek, opens
 *  the live socket (clip_ready hot-swap), and surfaces the §5.4 activity feed +
 *  the §7.2 conflict prompt so the reader can arbitrate a continuity dispute. */
export function useSyncEngine(sessionId: string | null): UseSyncEngineResult {
  const queryClient = useQueryClient();
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
          onSourceError: () => {
            // A clip/scene URL went dead (e.g. an expired presigned URL) — refetch
            // the shot list so fresh URLs replace it above the degraded bridge.
            void queryClient.invalidateQueries({
              predicate: (q) => q.queryKey.includes("shots"),
            });
          },
        },
      }),
    [sessionId, queryClient],
  );

  const snapshot = useSyncExternalStore(engine.subscribe, engine.getSnapshot);
  const [activity, setActivity] = useState<SessionActivity[]>([]);
  const [socketStatus, setSocketStatus] = useState<SocketStatus>("connecting");

  useEffect(() => {
    if (!sessionId) return;
    setActivity([]);
    setSocketStatus("connecting");
    let nextId = 0;

    const pushEvent = (event: KinoraEvent): void => {
      const previousClipUrl =
        event.event === "regen_done" ? engine.getClipUrl(event.shot_id) : undefined;
      const item = activityFromEvent(event, { id: nextId, at: Date.now(), previousClipUrl });
      if (!item) return;
      nextId += 1;
      setActivity((prev) => [item, ...prev].slice(0, MAX_SESSION_ACTIVITY));
    };

    const socket = new SessionSocket({
      baseUrl: API_BASE_URL,
      sessionId,
      getToken: () => authStore.getState().token,
      createWebSocket: (url) => new WebSocket(url) as unknown as WebSocketLike,
      onStatus: setSocketStatus,
      onEvent: (event) => {
        switch (event.event) {
          case "clip_ready":
            if (event.sync_segment) engine.ingestClip(event.sync_segment, event.oss_url ?? undefined);
            break;
          case "keyframe_ready":
            // §4.4 speculative bridge: cache the beat's still for client Ken-Burns.
            engine.ingestKeyframe(event.beat_id, event.oss_url ?? undefined);
            break;
          case "scene_stitched":
            // §9.6: once a scene is stitched, play the one continuous asset.
            engine.ingestScene(event.scene_id, event.oss_url, event.sync_map?.segments ?? []);
            break;
          case "budget_low":
            engine.noteBudgetLow(event.remaining_s);
            break;
          case "buffer_state":
            // §4.5/§12.4: refilling past the low watermark steps the ladder back up.
            engine.noteBufferState({
              committedSecondsAhead: event.committed_seconds_ahead,
              lowWatermarkS: event.low,
            });
            break;
          case "regen_done":
            // A resolved conflict (or canon edit) re-rendered this shot — hot-swap it.
            engine.swapClipUrl(event.shot_id, event.oss_url ?? null);
            break;
          default:
            break;
        }
        // Project every event into the §5.4 feed (drives the conflict prompt too).
        pushEvent(event);
      },
    });
    void socket.connect();
    return () => socket.close();
  }, [sessionId, engine]);

  const resolveConflict = useCallback(
    (conflictId: string, option: string) => {
      if (!sessionId) return;
      void api.POST("/api/sessions/{session_id}/conflict_choice", {
        params: { path: { session_id: sessionId } },
        body: { conflict_id: conflictId, option: option as components["schemas"]["ConflictOption"] },
      });
    },
    [sessionId],
  );

  return { engine, snapshot, activity, socketStatus, resolveConflict };
}
