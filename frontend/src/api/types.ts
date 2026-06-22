// DTOs for the Kinora backend (Phase 9). Base path is `/api`; the dev server
// proxies to http://localhost:8000. These mirror the real contract — field
// names match what the gateway returns. Genuinely optional / not-yet-confirmed
// fields are marked optional so the UI degrades gracefully rather than throwing.

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
export interface Credentials {
  email: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  token_type?: string;
}

export interface User {
  id: string;
  email: string;
  created_at?: string;
}

// ---------------------------------------------------------------------------
// Books / pages / canon / shots
// ---------------------------------------------------------------------------
export type BookStatus = "importing" | "ready" | "failed";

export interface Book {
  id: string;
  title: string;
  status: BookStatus;
  /** 0..1 ingest progress while status === "importing". */
  progress: number;
  num_pages: number;
  author?: string;
  cover_url?: string;
  /** Current ingest stage label, e.g. "analysing pages". */
  stage?: string;
  error?: string;
  created_at?: string;
}

/** A normalized word bounding box on a rasterised page: [x, y, w, h] in 0..1. */
export type Bbox = [number, number, number, number];

export interface WordBox {
  word_index: number;
  text: string;
  bbox: Bbox;
}

export interface Page {
  /** Presigned URL for the rasterised page image. */
  image_url: string;
  text: string;
  word_boxes: WordBox[];
  /** Intrinsic pixel size, when the backend reports it (used for layout). */
  width?: number;
  height?: number;
}

export interface SourceSpan {
  page: number;
  para?: number;
  /** Inclusive-start, exclusive-end global word indices. */
  word_range: [number, number];
}

export type ShotStatus =
  | "planned"
  | "keyframed"
  | "rendering"
  | "accepted"
  | "degraded"
  | "failed";

export interface ShotQA {
  ccs: number;
  style_drift: number;
  timeline_ok: boolean;
  motion_artifact?: number;
  score: number;
  verdict: "pass" | "fail";
  reason?: string;
}

export interface Shot {
  shot_id: string;
  beat_id: string;
  scene_id: string;
  source_span: SourceSpan;
  status: ShotStatus;
  qa?: ShotQA;
  est_duration_s?: number;
  duration_s?: number;
  /** Presigned clip URL once rendered & cached. */
  clip_url?: string;
  /** Presigned keyframe still for the Ken-Burns bridge. */
  keyframe_url?: string;
  reference_image_ids?: string[];
}

export type CanonEntityKind = "character" | "location" | "prop" | "style";

export interface ReferenceImage {
  oss_url: string;
  pose?: string;
  locked?: boolean;
}

export interface CanonEntity {
  id: string;
  type: CanonEntityKind;
  name: string;
  aliases?: string[];
  description?: string;
  appearance?: {
    description?: string;
    reference_images?: ReferenceImage[];
  };
  /** Style nodes carry palette / lens / art-direction tokens. */
  style_tokens?: Record<string, string>;
  voice?: {
    cosyvoice_voice_id?: string;
    reference_audio_url?: string;
  };
  version: number;
  valid_from_beat?: string;
  valid_to_beat?: string | null;
  first_appearance?: { page: number; beat_id: string };
}

export interface CanonGraph {
  entities: CanonEntity[];
  /** Optional Obsidian-style markdown vault export (kinora.md §8.1). */
  markdown?: string;
}

export interface CanonEditRequest {
  entity_key: string;
  changes: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Sessions & intent
// ---------------------------------------------------------------------------
export type SessionMode = "viewer" | "director";

export interface CreateSessionResponse {
  session_id: string;
}

export interface Session {
  session_id: string;
  book_id: string;
  focus_word?: number;
  velocity_wps?: number;
  committed_seconds_ahead?: number;
  budget_remaining_s?: number;
  mode?: SessionMode;
}

export interface IntentUpdate {
  focus_word: number;
  velocity: number;
  mode: SessionMode;
}

export interface CommentRequest {
  shot_id: string;
  /** base64-encoded PNG of the selected region. */
  region_png: string;
  note: string;
}

export interface ConflictChoiceRequest {
  conflict_id: string;
  option: string;
}

// ---------------------------------------------------------------------------
// Sync map (kinora.md §9.4) — binds video-time ↔ page ↔ word.
// ---------------------------------------------------------------------------
export interface SyncWord {
  word_index: number;
  text: string;
  t_start: number;
  t_end: number;
  bbox?: Bbox;
}

export interface SyncSegment {
  shot_id: string;
  video_start_s: number;
  video_end_s: number;
  page: number;
  /** When to flip the page — slightly before the shot ends. */
  page_turn_at_s: number;
  words: SyncWord[];
}

export interface SyncMap {
  scene_id: string;
  segments: SyncSegment[];
}

// ---------------------------------------------------------------------------
// Event channel (kinora.md §5.6)
// ---------------------------------------------------------------------------
export interface ConflictOption {
  id: string;
  action: string;
  cost_video_s?: number;
  requires?: string;
}

export interface Conflict {
  conflict_id: string;
  raised_by?: string;
  type?: string;
  shot_id?: string;
  claim?: string;
  canon_fact?: string;
  current_beat?: string;
  options: ConflictOption[];
}

export interface KeyframeReadyPayload {
  beat_id: string;
  oss_url: string;
  shot_id?: string;
}
export interface ClipReadyPayload {
  shot_id: string;
  oss_url: string;
  sync_segment: SyncSegment;
}
export interface SceneStitchedPayload {
  scene_id: string;
  oss_url: string;
  sync_map: SyncMap;
}
export interface RegenDonePayload {
  shot_id: string;
  oss_url: string;
  qa: ShotQA;
}
export interface BudgetLowPayload {
  remaining_s: number;
}
export interface AgentActivityPayload {
  agent: string;
  message: string;
  conflict?: Conflict;
}
export interface ConflictChoicePayload {
  conflict_id: string;
  options: ConflictOption[];
  claim?: string;
  canon_fact?: string;
  shot_id?: string;
}
export interface IngestProgressPayload {
  stage: string;
  pct: number;
  book_id?: string;
}

/** Discriminated union of every server-pushed event. */
export type KinoraEvent =
  | { type: "keyframe_ready"; data: KeyframeReadyPayload }
  | { type: "clip_ready"; data: ClipReadyPayload }
  | { type: "scene_stitched"; data: SceneStitchedPayload }
  | { type: "regen_done"; data: RegenDonePayload }
  | { type: "budget_low"; data: BudgetLowPayload }
  | { type: "agent_activity"; data: AgentActivityPayload }
  | { type: "conflict_choice"; data: ConflictChoicePayload }
  | { type: "ingest_progress"; data: IngestProgressPayload };

export type KinoraEventType = KinoraEvent["type"];

/** An event as stored in the feed, with a stable id and arrival time. */
export type StoredEvent = KinoraEvent & { id: string; receivedAt: number };

// ---------------------------------------------------------------------------
// Eval / metrics (Phase 11)
// ---------------------------------------------------------------------------
export interface BufferTracePoint {
  t: number;
  committed_seconds_ahead: number;
  low: number;
  high: number;
}

export interface MetricPair {
  crew: number;
  baseline: number;
}

export interface EvalReport {
  ccs: MetricPair;
  efficiency: MetricPair;
  regen_rate: MetricPair;
  style_drift: MetricPair;
}
