import { create } from "zustand";

import type {
  ClipReadyPayload,
  ConflictChoicePayload,
  KinoraEvent,
  StoredEvent,
} from "../api/types";
import type { ConnectionStatus } from "../sync/GenerationClient";

const FEED_CAP = 400;
let seq = 0;

interface EventsState {
  /** Every event received, oldest → newest, capped. */
  feed: StoredEvent[];
  /** Just agent_activity, for the live negotiation feed. */
  agentFeed: StoredEvent[];
  keyframesByBeat: Record<string, string>;
  keyframesByShot: Record<string, string>;
  clips: Record<string, ClipReadyPayload>;
  conflicts: ConflictChoicePayload[];
  budgetRemaining: number | null;
  ingestProgress: Record<string, { stage: string; pct: number }>;
  connection: ConnectionStatus;
  push: (event: KinoraEvent) => void;
  setConnection: (status: ConnectionStatus) => void;
  resolveConflict: (conflictId: string) => void;
  reset: () => void;
}

const initialState = {
  feed: [] as StoredEvent[],
  agentFeed: [] as StoredEvent[],
  keyframesByBeat: {} as Record<string, string>,
  keyframesByShot: {} as Record<string, string>,
  clips: {} as Record<string, ClipReadyPayload>,
  conflicts: [] as ConflictChoicePayload[],
  budgetRemaining: null as number | null,
  ingestProgress: {} as Record<string, { stage: string; pct: number }>,
  connection: "idle" as ConnectionStatus,
};

export const useEventsStore = create<EventsState>((set) => ({
  ...initialState,

  push: (event) =>
    set((state) => {
      seq += 1;
      const stored: StoredEvent = { ...event, id: `evt_${seq}`, receivedAt: Date.now() };
      const feed = [...state.feed, stored].slice(-FEED_CAP);
      const patch: Partial<EventsState> = { feed };

      switch (event.type) {
        case "keyframe_ready": {
          patch.keyframesByBeat = {
            ...state.keyframesByBeat,
            [event.data.beat_id]: event.data.oss_url,
          };
          if (event.data.shot_id) {
            patch.keyframesByShot = {
              ...state.keyframesByShot,
              [event.data.shot_id]: event.data.oss_url,
            };
          }
          break;
        }
        case "clip_ready":
          patch.clips = { ...state.clips, [event.data.shot_id]: event.data };
          break;
        case "regen_done":
          // A regenerated shot replaces the cached clip URL for that shot.
          patch.clips = {
            ...state.clips,
            [event.data.shot_id]: {
              shot_id: event.data.shot_id,
              oss_url: event.data.oss_url,
              sync_segment: state.clips[event.data.shot_id]?.sync_segment ?? {
                shot_id: event.data.shot_id,
                video_start_s: 0,
                video_end_s: 0,
                page: 0,
                page_turn_at_s: 0,
                words: [],
              },
            },
          };
          break;
        case "budget_low":
          patch.budgetRemaining = event.data.remaining_s;
          break;
        case "agent_activity":
          patch.agentFeed = [...state.agentFeed, stored].slice(-FEED_CAP);
          break;
        case "conflict_choice": {
          const exists = state.conflicts.some(
            (c) => c.conflict_id === event.data.conflict_id,
          );
          patch.conflicts = exists
            ? state.conflicts
            : [...state.conflicts, event.data];
          break;
        }
        case "ingest_progress":
          patch.ingestProgress = {
            ...state.ingestProgress,
            [event.data.book_id ?? "_"]: {
              stage: event.data.stage,
              pct: event.data.pct,
            },
          };
          break;
        case "scene_stitched":
        default:
          break;
      }
      return patch;
    }),

  setConnection: (connection) => set({ connection }),

  resolveConflict: (conflictId) =>
    set((state) => ({
      conflicts: state.conflicts.filter((c) => c.conflict_id !== conflictId),
    })),

  reset: () => set({ ...initialState }),
}));
