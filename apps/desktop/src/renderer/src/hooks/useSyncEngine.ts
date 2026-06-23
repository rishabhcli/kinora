import {
  SessionSocket,
  SyncEngine,
  type SyncSnapshot,
  type WebSocketLike,
} from "@kinora/core";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { api } from "../lib/api";
import { authStore } from "../lib/auth";
import { API_BASE_URL } from "../lib/config";

export interface SessionActivity {
  id: number;
  kind: "agent" | "budget" | "regen" | "conflict" | "scene";
  text: string;
}

export interface UseSyncEngineResult {
  engine: SyncEngine;
  snapshot: SyncSnapshot;
  activity: SessionActivity[];
  budgetRemaining: number | null;
  sendComment: (note: string, shotId?: string | null) => void;
}

const MAX_ACTIVITY = 50;

/**
 * Own the per-session SyncEngine: bind it into React, push debounced intent +
 * seeks to the Scheduler, open the live socket (clip_ready hot-swap), and
 * surface the crew's §5.6 activity + budget for the Director panel.
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
  const [activity, setActivity] = useState<SessionActivity[]>([]);
  const [budgetRemaining, setBudgetRemaining] = useState<number | null>(null);
  const socketRef = useRef<SessionSocket | null>(null);

  useEffect(() => {
    if (!sessionId) return;
    let nextId = 0;
    const push = (kind: SessionActivity["kind"], text: string): void =>
      setActivity((prev) => [{ id: nextId++, kind, text }, ...prev].slice(0, MAX_ACTIVITY));

    const socket = new SessionSocket({
      baseUrl: API_BASE_URL,
      sessionId,
      getToken: () => authStore.getState().token,
      createWebSocket: (url) => new WebSocket(url) as unknown as WebSocketLike,
      onEvent: (event) => {
        switch (event.event) {
          case "clip_ready":
            if (event.sync_segment) engine.ingestClip(event.sync_segment, event.oss_url ?? undefined);
            break;
          case "agent_activity":
            push("agent", `${event.agent}${event.aspect ? ` · ${event.aspect}` : ""}: ${event.message}`);
            break;
          case "budget_low":
            setBudgetRemaining(event.remaining_s);
            push("budget", `Budget low — ${Math.round(event.remaining_s)}s of video left`);
            break;
          case "regen_done":
            push("regen", `Shot ${event.shot_id} regenerated`);
            break;
          case "conflict_choice":
            push("conflict", `Conflict ${event.conflict_id} needs a decision`);
            break;
          case "scene_stitched":
            push("scene", `Scene ${event.scene_id} stitched`);
            break;
          default:
            break;
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

  const sendComment = useCallback((note: string, shotId?: string | null) => {
    socketRef.current?.sendComment(note, shotId);
  }, []);

  return { engine, snapshot, activity, budgetRemaining, sendComment };
}
