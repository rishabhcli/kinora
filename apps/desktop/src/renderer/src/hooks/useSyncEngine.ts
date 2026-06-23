import {
  activityFromEvent,
  type BufferZone,
  type CommentResponse,
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
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import type { ShotUpdateMap } from "../components/director/shots";
import { api } from "../lib/api";
import { authStore } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

/** Live committed-buffer surfacing for the §5.3 buffer hairline + zone badge. */
export interface BufferState {
  committedAheadS: number;
  low: number;
  high: number;
  commitHorizon: number;
  bursting: boolean;
  idle: boolean;
  zone: BufferZone;
  /** Reading-seconds to the nearest upcoming shot (what `zone` was derived from). */
  etaNextS: number | null;
  /** The clamped scheduler velocity (wps) — authoritative over the client estimate. */
  velocityWps: number | null;
  /** In-flight render counts (committed full-video / speculative keyframes). */
  inflightCommitted: number;
  inflightSpeculative: number;
  /** Shots promoted to full video on the last tick — a real generation burst. */
  promoted: number;
  budgetRemainingS: number | null;
}

export interface UseSyncEngineResult {
  engine: SyncEngine;
  snapshot: SyncSnapshot;
  /** The §5.4 live agent-activity feed, newest first (capped at 50). */
  activity: SessionActivity[];
  budgetRemaining: number | null;
  /** Live connection state for the feed's link indicator. */
  socketStatus: SocketStatus;
  /** The §5.3 committed-buffer state for the hairline + zone badge (null until first tick). */
  bufferState: BufferState | null;
  /** Per-shot clip/QA/"regenerating" state layered over the fetched shot list. */
  shotUpdates: ShotUpdateMap;
  /**
   * Mark a batch of shots invalidated/rendering in the shared `shotUpdates` map.
   * A Director canon edit calls this with its dependent `affected_shot_ids` so the
   * shot timeline shows them re-rendering until each `regen_done` lands (§8.7).
   */
  markRegenerating: (shotIds: string[]) => void;
  /**
   * Send a Director region-comment through the regen-triggering REST path
   * (`POST /sessions/{id}/comment`): it classifies the note, re-rolls the shot's
   * seed, and enqueues exactly that shot for a fresh take (§5.4/§8.7). Resolves
   * with the routing, or `null` if there's no session/shot to target.
   */
  sendComment: (
    note: string,
    shotId: string | null,
    regionPng?: string | null,
  ) => Promise<CommentResponse | null>;
  /** Record the Director's resolution of a surfaced conflict (§7.2). */
  resolveConflict: (conflictId: string, option: string) => void;
}

/**
 * Own the per-session SyncEngine: bind it into React, push debounced intent +
 * seeks to the Scheduler, open the live socket (clip_ready hot-swap), and
 * surface the crew's §5.6 activity + budget + link state for the feed (§5.4).
 */
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
            // A clip/scene URL went dead (e.g. an expired presigned URL past its
            // 1h TTL). Refetch the shot list so fresh URLs replace it — the engine
            // is already showing the degraded bridge, so this recovers above it.
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
  const [budgetRemaining, setBudgetRemaining] = useState<number | null>(null);
  const [socketStatus, setSocketStatus] = useState<SocketStatus>("connecting");
  const [bufferState, setBufferState] = useState<BufferState | null>(null);
  const [shotUpdates, setShotUpdates] = useState<ShotUpdateMap>({});
  const socketRef = useRef<SessionSocket | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    // Fresh session → fresh feed (and id space, so React keys never collide).
    setActivity([]);
    setBudgetRemaining(null);
    setSocketStatus("connecting");
    setShotUpdates({});
    setBufferState(null);
    let nextId = 0;

    const pushEvent = (event: KinoraEvent): void => {
      // Read the pre-regen clip *before* the engine swaps it, for before/after.
      const previousClipUrl =
        event.event === "regen_done" ? engine.getClipUrl(event.shot_id) : undefined;
      const item = activityFromEvent(event, { id: nextId, at: Date.now(), previousClipUrl });
      if (!item) return;
      nextId += 1;
      setActivity((prev) => [item, ...prev].slice(0, MAX_SESSION_ACTIVITY));
    };

    // Refresh a timeline tile's clip + QA, clearing any "regenerating" flag.
    const patchShot = (
      shotId: string,
      clipUrl: string | null,
      qa: Record<string, unknown> | null,
    ): void =>
      setShotUpdates((prev) => ({
        ...prev,
        [shotId]: { clipUrl: clipUrl ?? prev[shotId]?.clipUrl ?? null, qa, status: "ready" },
      }));

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
            else if (event.oss_url) engine.swapClipUrl(event.shot_id, event.oss_url);
            patchShot(event.shot_id, event.oss_url ?? null, event.qa ?? null);
            break;
          case "keyframe_ready":
            // §4.4 speculative bridge: cache the beat's still for client Ken-Burns.
            engine.ingestKeyframe(event.beat_id, event.oss_url ?? undefined);
            break;
          case "scene_stitched":
            // §9.6: prefer the one stitched scene asset over per-shot clips for
            // its word range — within a scene playback is then gapless.
            engine.ingestScene(event.scene_id, event.oss_url, event.sync_map?.segments ?? []);
            break;
          case "buffer_state":
            // §5.3: live committed-buffer occupancy + zone for the hairline/badge.
            // Note: budget_remaining_s rides along here, but we deliberately do not
            // touch `budgetRemaining` (its non-null value means "low" to the rest of
            // the app — only `budget_low` should set it).
            setBufferState({
              committedAheadS: event.committed_seconds_ahead,
              low: event.low,
              high: event.high,
              commitHorizon: event.commit_horizon,
              bursting: event.bursting,
              idle: event.idle,
              zone: event.zone,
              etaNextS: event.eta_next_s ?? null,
              velocityWps: event.velocity_wps ?? null,
              inflightCommitted: event.inflight_committed ?? 0,
              inflightSpeculative: event.inflight_speculative ?? 0,
              promoted: event.promoted ?? 0,
              budgetRemainingS: event.budget_remaining_s ?? null,
            });
            // The ladder steps back up once the committed buffer refills (§12.4).
            engine.noteBufferState({
              committedSecondsAhead: event.committed_seconds_ahead,
              lowWatermarkS: event.low,
            });
            break;
          case "budget_low":
            setBudgetRemaining(event.remaining_s);
            engine.noteBudgetLow(event.remaining_s);
            break;
          default:
            break;
        }
        // Project into the feed (captures the regen "before" frame as it goes).
        pushEvent(event);
        // Then hot-swap the regenerated shot in place (the canon panel + timeline
        // read the same shotUpdates, so both flip from rendering → ready here).
        if (event.event === "regen_done") {
          engine.swapClipUrl(event.shot_id, event.oss_url ?? null);
          patchShot(event.shot_id, event.oss_url ?? null, event.qa ?? null);
        }
      },
    });
    socketRef.current = socket;
    void socket.connect();
    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [sessionId, engine]);

  const sendComment = useCallback(
    async (
      note: string,
      shotId: string | null,
      regionPng?: string | null,
    ): Promise<CommentResponse | null> => {
      if (!sessionId || !shotId) return null;
      const { data } = await api.POST("/api/sessions/{session_id}/comment", {
        params: { path: { session_id: sessionId } },
        body: { shot_id: shotId, note, region_png: regionPng ?? null },
      });
      if (data) {
        // Optimistically flip the targeted tile to "rendering" until clip_ready /
        // regen_done lands; the swap clears it back to a QA badge.
        setShotUpdates((prev) => ({ ...prev, [shotId]: { ...prev[shotId], status: "regenerating" } }));
      }
      return data ?? null;
    },
    [sessionId],
  );

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

  // A canon edit marks its dependent shots regenerating in the shared map; the
  // socket's regen_done handler clears each back to "ready" with its fresh clip.
  const markRegenerating = useCallback((shotIds: string[]) => {
    if (shotIds.length === 0) return;
    setShotUpdates((prev) => {
      const next = { ...prev };
      for (const id of shotIds) next[id] = { ...next[id], status: "regenerating" };
      return next;
    });
  }, []);

  return {
    engine,
    snapshot,
    activity,
    budgetRemaining,
    socketStatus,
    bufferState,
    shotUpdates,
    markRegenerating,
    sendComment,
    resolveConflict,
  };
}
