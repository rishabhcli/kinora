/**
 * The §5.6 session-event channel, modeled as Zod schemas so both shells parse
 * the SSE/WebSocket stream safely. Shapes mirror the backend publish sites
 * (queue/worker.py, scheduler/*.py, api/routes/*.py); unknown event types parse
 * to `null` for forward-compatibility.
 */
import { z } from "zod";

// --- Sync map (§9.4): the video-time <-> page <-> word binding -------------- #

export const syncWordSchema = z.object({
  word_index: z.number().int(),
  text: z.string(),
  t_start: z.number(),
  t_end: z.number(),
  /** Normalized [x, y, w, h] page box, or null when the page has no box. */
  bbox: z.array(z.number()).length(4).nullable().optional(),
});
export type SyncWord = z.infer<typeof syncWordSchema>;

export const syncSegmentSchema = z.object({
  shot_id: z.string(),
  video_start_s: z.number(),
  video_end_s: z.number(),
  page: z.number().int(),
  page_turn_at_s: z.number(),
  words: z.array(syncWordSchema).default([]),
});
export type SyncSegment = z.infer<typeof syncSegmentSchema>;

/** Stitched-scene sync map (scene_stitched). Tolerant of extra backend fields. */
export const sceneSyncMapSchema = z
  .object({ segments: z.array(syncSegmentSchema).optional() })
  .passthrough();
export type SceneSyncMap = z.infer<typeof sceneSyncMapSchema>;

// --- The event union -------------------------------------------------------- #

export const clipReadyEvent = z.object({
  event: z.literal("clip_ready"),
  shot_id: z.string(),
  clip_key: z.string().nullable().optional(),
  oss_url: z.string().nullable().optional(),
  sync_segment: syncSegmentSchema.nullable().optional(),
  qa: z.record(z.unknown()).nullable().optional(),
  rung: z.string().nullable().optional(),
  video_seconds: z.number().nullable().optional(),
});

export const sceneStitchedEvent = z.object({
  event: z.literal("scene_stitched"),
  scene_id: z.string(),
  oss_url: z.string().nullable().optional(),
  sync_map: sceneSyncMapSchema.nullable().optional(),
});

export const keyframeReadyEvent = z.object({
  event: z.literal("keyframe_ready"),
  beat_id: z.string(),
  oss_url: z.string().nullable().optional(),
});

export const regenDoneEvent = z.object({
  event: z.literal("regen_done"),
  shot_id: z.string(),
  oss_url: z.string().nullable().optional(),
  qa: z.record(z.unknown()).nullable().optional(),
});

export const budgetLowEvent = z.object({
  event: z.literal("budget_low"),
  remaining_s: z.number(),
});

export const agentActivityEvent = z.object({
  event: z.literal("agent_activity"),
  agent: z.string(),
  aspect: z.string().nullable().optional(),
  message: z.string(),
  shot_id: z.string().nullable().optional(),
});

export const conflictChoiceEvent = z.object({
  event: z.literal("conflict_choice"),
  conflict_id: z.string(),
  options: z.array(z.unknown()).default([]),
  claim: z.unknown().optional(),
  canon_fact: z.unknown().optional(),
  shot_id: z.string().nullable().optional(),
});

export const ingestProgressEvent = z
  .object({ event: z.literal("ingest_progress"), book_id: z.string() })
  .passthrough();

export const kinoraEventSchema = z.discriminatedUnion("event", [
  clipReadyEvent,
  sceneStitchedEvent,
  keyframeReadyEvent,
  regenDoneEvent,
  budgetLowEvent,
  agentActivityEvent,
  conflictChoiceEvent,
  ingestProgressEvent,
]);
export type KinoraEvent = z.infer<typeof kinoraEventSchema>;
export type KinoraEventType = KinoraEvent["event"];

/** Parse a raw SSE/WS payload into a typed event, or `null` if unrecognized. */
export function parseSessionEvent(raw: unknown): KinoraEvent | null {
  const result = kinoraEventSchema.safeParse(raw);
  return result.success ? result.data : null;
}
