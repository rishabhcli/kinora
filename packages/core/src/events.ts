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
  /**
   * Separate narration track (CosyVoice .wav), when the backend exposes it apart
   * from the muxed clip — the bottom rung of the §12.4 ladder (audio + karaoke
   * text) can play this with no video. Forward-compatible: absent today.
   */
  audio_url: z.string().nullable().optional(),
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

/** The viewer-facing representation of the nearest upcoming shot (§4.4/§5.3). */
export const bufferZoneSchema = z.enum(["committed", "speculative", "cold"]);
export type BufferZone = z.infer<typeof bufferZoneSchema>;

/**
 * Live buffer surfacing (§5.3): the committed video-seconds the hairline fills
 * toward `high`, the watermarks it is measured against, the burst/idle hysteresis
 * flags, and the zone badge. Emitted once per Scheduler control tick (§4.9).
 */
export const bufferStateEvent = z.object({
  event: z.literal("buffer_state"),
  committed_seconds_ahead: z.number(),
  low: z.number(),
  high: z.number(),
  commit_horizon: z.number(),
  bursting: z.boolean().default(false),
  idle: z.boolean().default(false),
  zone: bufferZoneSchema.default("cold"),
  /** Reading-seconds to the nearest upcoming shot (the value `zone` was derived from). */
  eta_next_s: z.number().nullable().optional(),
  /** The clamped reading velocity the scheduler is planning against (wps). */
  velocity_wps: z.number().optional(),
  /** In-flight render counts: committed full-video shots / speculative keyframes. */
  inflight_committed: z.number().int().optional(),
  inflight_speculative: z.number().int().optional(),
  /** Shots promoted to full video on this tick — a real generation burst. */
  promoted: z.number().int().default(0),
  budget_remaining_s: z.number().nullable().optional(),
});
export type BufferStateEvent = z.infer<typeof bufferStateEvent>;

// --- Conflict negotiation (§7.2): structured Showrunner ↔ Continuity dispute - #

/** The fixed policy resolutions the Showrunner arbitrates between (§7.2). */
export const conflictOptionIdSchema = z.enum(["honor_canon", "surface_to_user", "evolve_canon"]);
export type ConflictOptionId = z.infer<typeof conflictOptionIdSchema>;

/** One option on a surfaced conflict, with its budget cost / precondition. */
export const conflictOptionSchema = z.object({
  /** Tolerant of an unknown id so a future policy option still renders. */
  id: z.union([conflictOptionIdSchema, z.string()]),
  action: z.string(),
  cost_video_s: z.number().nullable().optional(),
  requires: z.string().nullable().optional(),
});
export type ConflictOption = z.infer<typeof conflictOptionSchema>;

/** The Showrunner's decision record / a director choice ack on agent_activity. */
export const conflictDecisionSchema = z
  .object({
    conflict_id: z.string().optional(),
    /** Set by the director's pick (POST conflict_choice). */
    option: z.union([conflictOptionIdSchema, z.string()]).nullable().optional(),
    /** Set by the Showrunner's auto-arbitration (DecisionRecord). */
    chosen_option: z.union([conflictOptionIdSchema, z.string()]).nullable().optional(),
    reasoning: z.string().nullable().optional(),
    claim: z.string().nullable().optional(),
    canon_fact: z.string().nullable().optional(),
    evolved_canon: z.boolean().optional(),
  })
  .passthrough();
export type ConflictDecision = z.infer<typeof conflictDecisionSchema>;

export const agentActivityEvent = z.object({
  event: z.literal("agent_activity"),
  agent: z.string(),
  aspect: z.string().nullable().optional(),
  message: z.string(),
  shot_id: z.string().nullable().optional(),
  job_id: z.string().nullable().optional(),
  /** Present when the activity records a §7.2 conflict decision (auto or director). */
  conflict: conflictDecisionSchema.nullable().optional(),
  /** Structured Critic QA (ccs/verdict) when the activity reports a QA result (§9.5). */
  qa: z.record(z.unknown()).nullable().optional(),
});

export const conflictChoiceEvent = z.object({
  event: z.literal("conflict_choice"),
  conflict_id: z.string(),
  options: z.array(conflictOptionSchema).default([]),
  claim: z.string().nullable().optional(),
  canon_fact: z.string().nullable().optional(),
  current_beat: z.string().nullable().optional(),
  raised_by: z.string().nullable().optional(),
  shot_id: z.string().nullable().optional(),
});

export const ingestProgressEvent = z.object({
  event: z.literal("ingest_progress"),
  book_id: z.string(),
  stage: z.string().optional(),
  pct: z.number().optional(),
});

export const kinoraEventSchema = z.discriminatedUnion("event", [
  clipReadyEvent,
  sceneStitchedEvent,
  keyframeReadyEvent,
  regenDoneEvent,
  budgetLowEvent,
  bufferStateEvent,
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
