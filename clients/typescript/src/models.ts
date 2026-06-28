/**
 * Typed wire models for the Kinora API.
 *
 * These mirror `backend/app/api/schemas.py` and `backend/app/films/contract.py`.
 * Every response interface carries an index signature so an SDK build never
 * breaks when the backend adds a field (forward-compatible); request types are
 * exact. Keep these in sync via `clients/contract-drift/check_drift.py`.
 */

/** A passthrough for arbitrary JSON objects (canon style tokens, qa, etc.). */
export type Json = Record<string, unknown>;

// --------------------------------------------------------------------------- //
// Auth
// --------------------------------------------------------------------------- //

export interface RegisterRequest {
  email: string;
  password: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface UserResponse {
  id: string;
  email: string;
  created_at: string | null;
  [extra: string]: unknown;
}

// --------------------------------------------------------------------------- //
// Books
// --------------------------------------------------------------------------- //

export type BookStatus = "importing" | "ready" | "failed" | (string & {});

export interface BookResponse {
  id: string;
  title: string;
  author: string | null;
  status: BookStatus;
  num_pages: number | null;
  art_direction: string | null;
  created_at: string | null;
  /** Import progress in [0, 1], or null when unknown. */
  progress: number | null;
  /** Current ingest stage label. */
  stage: string | null;
  /** Presigned cover URL, or null when none yet. */
  cover_url: string | null;
  [extra: string]: unknown;
}

/** A normalized [x0, y0, x1, y1] box in [0, 1] page-relative coordinates. */
export type Bbox = [number, number, number, number];

export interface WordBox {
  word_index: number;
  text: string;
  bbox: Bbox;
  [extra: string]: unknown;
}

export interface PageResponse {
  book_id: string;
  page_number: number;
  image_url: string | null;
  text: string | null;
  word_boxes: WordBox[];
  [extra: string]: unknown;
}

/** A `[start, end)` half-open word range in the book-global word index. */
export interface SourceSpan {
  page?: number;
  para?: number;
  word_range?: [number, number];
  [extra: string]: unknown;
}

export type ShotStatus =
  | "planned"
  | "promoted"
  | "rendering"
  | "accepted"
  | "rejected"
  | "failed"
  | (string & {});

export interface ShotResponse {
  shot_id: string;
  beat_id: string | null;
  scene_id: string | null;
  source_span: SourceSpan | null;
  status: ShotStatus;
  render_mode: string | null;
  duration_s: number | null;
  qa: Json | null;
  clip_url: string | null;
  reference_image_ids: string[];
  [extra: string]: unknown;
}

// ----- canon -----

export interface CanonReferenceImage {
  oss_url: string;
  oss_key: string | null;
  pose: string | null;
  locked: boolean | null;
  [extra: string]: unknown;
}

export interface CanonAppearance {
  description: string | null;
  reference_images: CanonReferenceImage[];
  [extra: string]: unknown;
}

export interface CanonEntityResponse {
  id: string;
  type: string;
  name: string;
  aliases: string[];
  description: string | null;
  appearance: CanonAppearance | null;
  style_tokens: Json | null;
  voice: Json | null;
  version: number;
  valid_from_beat: number | null;
  valid_to_beat: number | null;
  first_appearance: Json | null;
  [extra: string]: unknown;
}

export interface CanonStateResponse {
  id: string;
  subject_entity_key: string;
  predicate: string;
  object_value: string;
  valid_from_beat: number;
  valid_to_beat: number | null;
  version: number;
  active: boolean;
  source_span: Json | null;
  [extra: string]: unknown;
}

export interface CanonResponse {
  book_id: string;
  entities: CanonEntityResponse[];
  states: CanonStateResponse[];
  markdown: string | null;
  [extra: string]: unknown;
}

// --------------------------------------------------------------------------- //
// Films / sync map
// --------------------------------------------------------------------------- //

export interface SyncWord {
  word_index: number;
  text: string;
  t_start: number;
  t_end: number;
  bbox: number[] | null;
  [extra: string]: unknown;
}

export interface FilmSyncSegment {
  shot_id: string;
  scene_id: string;
  word_range: [number, number];
  t_start_s: number;
  t_end_s: number;
  page: number;
  page_turn_at_s: number;
  words: SyncWord[];
  [extra: string]: unknown;
}

export interface FilmSyncMap {
  scene_id: string;
  duration_s: number;
  segments: FilmSyncSegment[];
  [extra: string]: unknown;
}

export interface SceneRef {
  scene_id: string;
  scene_index: number;
  word_range: [number, number];
  stitched: boolean;
  duration_s: number | null;
  [extra: string]: unknown;
}

export interface SceneFilm {
  scene_id: string;
  event_id: string;
  book_id: string;
  scene_index: number;
  event_index: number;
  page_start: number;
  page_end: number;
  word_range: [number, number];
  stitched: boolean;
  oss_url: string | null;
  url_expires_at: string | null;
  duration_s: number | null;
  shot_count: number;
  sync_map: FilmSyncMap;
  [extra: string]: unknown;
}

export interface EventFilm {
  event_id: string;
  event_index: number;
  book_id: string;
  page_start: number;
  page_end: number;
  word_range: [number, number];
  stitched: boolean;
  oss_url: string | null;
  url_expires_at: string | null;
  duration_s: number | null;
  shot_count: number;
  sync_map: FilmSyncMap;
  scenes: SceneRef[];
  [extra: string]: unknown;
}

export interface RestoreState {
  session_id: string;
  focus_word: number;
  current_event_index: number | null;
  current_scene_id: string | null;
  mode: string;
  [extra: string]: unknown;
}

export interface EventsResponse {
  book_id: string;
  url_ttl_s: number;
  events: EventFilm[];
  restore: RestoreState | null;
  [extra: string]: unknown;
}

// --------------------------------------------------------------------------- //
// Sessions / intent / seek
// --------------------------------------------------------------------------- //

export type SessionMode = "viewer" | "director";

export interface CreateSessionRequest {
  book_id: string;
  focus_word?: number;
  mode?: SessionMode;
}

export interface SessionResponse {
  session_id: string;
  book_id: string;
  focus_word: number;
  velocity_wps: number;
  mode: SessionMode;
  committed_seconds_ahead: number;
  bursting: boolean;
  budget_remaining_s: number | null;
  inflight: Record<string, string[]>;
  [extra: string]: unknown;
}

export interface IntentRequest {
  focus_word: number;
  velocity?: number;
  mode?: SessionMode;
}

export interface IntentResponse {
  session_id: string;
  settled: boolean;
  allow_promotion: boolean;
  idle: boolean;
  bursting: boolean;
  committed_seconds_ahead: number;
  promoted: string[];
  keyframed: string[];
  cancelled: number;
  [extra: string]: unknown;
}

export interface SeekRequest {
  word: number;
}

export interface SeekResponse {
  session_id: string;
  word: number;
  cancelled: number;
  bridge_beat: string | null;
  committed_seconds_ahead: number;
  [extra: string]: unknown;
}

// --------------------------------------------------------------------------- //
// Director tools
// --------------------------------------------------------------------------- //

export interface CommentRequest {
  shot_id: string;
  note: string;
  /** base64-encoded PNG of the selected region. */
  region_png?: string;
}

export interface DirectingPriorView {
  kind: string;
  bias: number;
  weight: number;
  label: string;
  detail: string;
  applied: boolean;
  applied_value: string | null;
  last_note: string | null;
  [extra: string]: unknown;
}

export interface CommentResponse {
  shot_id: string;
  agent: string;
  aspect: string;
  message: string;
  job_id: string | null;
  learned: DirectingPriorView[];
  [extra: string]: unknown;
}

export interface CanonEditRequest {
  entity_key: string;
  changes: Json;
  valid_from_beat?: number;
}

export interface CanonEditResponse {
  entity_key: string;
  version: number;
  affected_shot_ids: string[];
  skipped_shots: number;
  [extra: string]: unknown;
}

export type ConflictOption = "honor_canon" | "evolve_canon" | "surface_to_user";

export interface ConflictChoiceRequest {
  conflict_id: string;
  option: ConflictOption;
}

export type ConflictStatus = "applied" | "deferred" | "already_resolved" | "recorded" | (string & {});

export interface ConflictChoiceResponse {
  conflict_id: string;
  option: ConflictOption;
  status: ConflictStatus;
  shot_id: string | null;
  reasoning: string | null;
  [extra: string]: unknown;
}

export interface ConflictRecordResponse {
  conflict_id: string;
  shot_id: string | null;
  claim: string | null;
  canon_fact: string | null;
  raised_by: string | null;
  current_beat: string | null;
  options: Json[];
  resolved: boolean;
  chosen_option: string | null;
  reasoning: string | null;
  [extra: string]: unknown;
}

// --------------------------------------------------------------------------- //
// Preferences
// --------------------------------------------------------------------------- //

export interface DirectingStyleResponse {
  scope: "user" | "book" | (string & {});
  book_id: string | null;
  priors: DirectingPriorView[];
  [extra: string]: unknown;
}

export interface ResetPrefsResponse {
  scope: string;
  book_id: string | null;
  cleared: number;
  [extra: string]: unknown;
}

// --------------------------------------------------------------------------- //
// Eval / optim
// --------------------------------------------------------------------------- //

export interface BufferTracePoint {
  t: number;
  committed_seconds_ahead: number;
  low: number;
  high: number;
  [extra: string]: unknown;
}

/** The eval report and cost/perf rollups are open-shaped server objects. */
export type EvalReport = Json;
export type CostReport = Json;
export type PerfReport = Json;

// --------------------------------------------------------------------------- //
// Errors
// --------------------------------------------------------------------------- //

export interface ErrorBody {
  type: string;
  message: string;
  detail?: Json | null;
}

export interface ErrorResponse {
  error: ErrorBody;
}
