/**
 * GENERATED FILE — do not edit by hand.
 *
 * Emitted from clients/spec/catalog.mjs by `node clients/spec/sync-ts.mjs`.
 * The catalog is the single source of truth for the Kinora API surface; this
 * typed view keeps the TS SDK self-contained.
 */

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface EndpointSpec {
  readonly id: string;
  readonly method: HttpMethod;
  readonly path: string;
  readonly tag: string;
  readonly auth: boolean;
  readonly summary: string;
  readonly requestModel: string | null;
  readonly responseModel: string | null;
  readonly status: number;
  readonly query?: Readonly<Record<string, string>>;
}

export interface EventSpec {
  readonly name: string;
  readonly summary: string;
  readonly fields: readonly string[];
  readonly channels: readonly ("session" | "book" | "library")[];
}

export interface ErrorTypeSpec {
  readonly type: string;
  readonly status: number;
  readonly summary: string;
}

export const API_VERSION = "1.0.0";
export const API_PREFIX = "/api";
export const DEFAULT_BASE_URL = "http://localhost:8000";

export const ENDPOINTS: readonly EndpointSpec[] = [
  {
    "id": "register",
    "method": "POST",
    "path": "/auth/register",
    "tag": "auth",
    "auth": false,
    "summary": "Create an account (email + password >= 8 chars).",
    "requestModel": "RegisterRequest",
    "responseModel": "UserResponse",
    "status": 201
  },
  {
    "id": "login",
    "method": "POST",
    "path": "/auth/login",
    "tag": "auth",
    "auth": false,
    "summary": "Exchange credentials for a bearer access token.",
    "requestModel": "LoginRequest",
    "responseModel": "TokenResponse",
    "status": 200
  },
  {
    "id": "me",
    "method": "GET",
    "path": "/auth/me",
    "tag": "auth",
    "auth": true,
    "summary": "Return the authenticated user.",
    "requestModel": null,
    "responseModel": "UserResponse",
    "status": 200
  },
  {
    "id": "uploadBook",
    "method": "POST",
    "path": "/books",
    "tag": "books",
    "auth": true,
    "summary": "Upload a PDF/EPUB and trigger Phase-A ingest. Multipart form.",
    "requestModel": "multipart",
    "responseModel": "BookResponse",
    "status": 201
  },
  {
    "id": "listBooks",
    "method": "GET",
    "path": "/books",
    "tag": "books",
    "auth": true,
    "summary": "List the books the current user owns (the shelf), newest first.",
    "requestModel": null,
    "responseModel": "BookResponse[]",
    "status": 200
  },
  {
    "id": "getBook",
    "method": "GET",
    "path": "/books/{book_id}",
    "tag": "books",
    "auth": true,
    "summary": "Fetch one book with its import status + progress.",
    "requestModel": null,
    "responseModel": "BookResponse",
    "status": 200
  },
  {
    "id": "getPage",
    "method": "GET",
    "path": "/books/{book_id}/pages/{page_number}",
    "tag": "books",
    "auth": true,
    "summary": "A page's presigned image URL, text, and per-word boxes.",
    "requestModel": null,
    "responseModel": "PageResponse",
    "status": 200
  },
  {
    "id": "getCanon",
    "method": "GET",
    "path": "/books/{book_id}/canon",
    "tag": "books",
    "auth": true,
    "summary": "The book's canon graph: entities, continuity facts, markdown vault.",
    "requestModel": null,
    "responseModel": "CanonResponse",
    "status": 200
  },
  {
    "id": "listShots",
    "method": "GET",
    "path": "/books/{book_id}/shots",
    "tag": "books",
    "auth": true,
    "summary": "The book's shots (the shot timeline) as a bare array.",
    "requestModel": null,
    "responseModel": "ShotResponse[]",
    "status": 200
  },
  {
    "id": "getBookCover",
    "method": "GET",
    "path": "/books/{book_id}/cover",
    "tag": "books",
    "auth": true,
    "summary": "302-redirect to the presigned cover image for an owned book.",
    "requestModel": null,
    "responseModel": null,
    "status": 302
  },
  {
    "id": "listEvents",
    "method": "GET",
    "path": "/books/{book_id}/events",
    "tag": "films",
    "auth": true,
    "summary": "Every event (scene) film — stitched URL + sync map + restore state.",
    "requestModel": null,
    "responseModel": "EventsResponse",
    "status": 200
  },
  {
    "id": "getSceneFilm",
    "method": "GET",
    "path": "/books/{book_id}/scenes/{scene_id}/film",
    "tag": "films",
    "auth": true,
    "summary": "One scene's film (partial load).",
    "requestModel": null,
    "responseModel": "SceneFilm",
    "status": 200
  },
  {
    "id": "createSession",
    "method": "POST",
    "path": "/sessions",
    "tag": "sessions",
    "auth": true,
    "summary": "Open a reading session against a book.",
    "requestModel": "CreateSessionRequest",
    "responseModel": "SessionResponse",
    "status": 201
  },
  {
    "id": "getSession",
    "method": "GET",
    "path": "/sessions/{session_id}",
    "tag": "sessions",
    "auth": true,
    "summary": "Return the Scheduler's live control state for a session.",
    "requestModel": null,
    "responseModel": "SessionResponse",
    "status": 200
  },
  {
    "id": "updateIntent",
    "method": "POST",
    "path": "/sessions/{session_id}/intent",
    "tag": "sessions",
    "auth": true,
    "summary": "Apply a debounced reading-intent update and run one control tick.",
    "requestModel": "IntentRequest",
    "responseModel": "IntentResponse",
    "status": 200
  },
  {
    "id": "seek",
    "method": "POST",
    "path": "/sessions/{session_id}/seek",
    "tag": "sessions",
    "auth": true,
    "summary": "Jump to a word: cancel distant work, bridge keyframe, re-seed.",
    "requestModel": "SeekRequest",
    "responseModel": "SeekResponse",
    "status": 200
  },
  {
    "id": "comment",
    "method": "POST",
    "path": "/sessions/{session_id}/comment",
    "tag": "director",
    "auth": true,
    "summary": "Classify a Director region-comment, enqueue a regen, emit agent_activity.",
    "requestModel": "CommentRequest",
    "responseModel": "CommentResponse",
    "status": 200
  },
  {
    "id": "canonEdit",
    "method": "POST",
    "path": "/books/{book_id}/canon_edit",
    "tag": "director",
    "auth": true,
    "summary": "Edit a canon entity and surgically regen only the dependent shots.",
    "requestModel": "CanonEditRequest",
    "responseModel": "CanonEditResponse",
    "status": 200
  },
  {
    "id": "conflictChoice",
    "method": "POST",
    "path": "/sessions/{session_id}/conflict_choice",
    "tag": "director",
    "auth": true,
    "summary": "Apply the Director's resolution of a surfaced continuity conflict.",
    "requestModel": "ConflictChoiceRequest",
    "responseModel": "ConflictChoiceResponse",
    "status": 200
  },
  {
    "id": "listConflicts",
    "method": "GET",
    "path": "/sessions/{session_id}/conflicts",
    "tag": "director",
    "auth": true,
    "summary": "The session's conflict log — surfaced disputes + their resolutions.",
    "requestModel": null,
    "responseModel": "ConflictRecordResponse[]",
    "status": 200
  },
  {
    "id": "demoConflict",
    "method": "POST",
    "path": "/sessions/{session_id}/demo/conflict",
    "tag": "director",
    "auth": true,
    "summary": "DEV-ONLY: surface the canonical lost-sword conflict (local env only).",
    "requestModel": null,
    "responseModel": "ConflictRecordResponse",
    "status": 200
  },
  {
    "id": "getMyPrefs",
    "method": "GET",
    "path": "/me/prefs",
    "tag": "prefs",
    "auth": true,
    "summary": "The reader's accumulated directing style across all their books.",
    "requestModel": null,
    "responseModel": "DirectingStyleResponse",
    "status": 200
  },
  {
    "id": "getBookPrefs",
    "method": "GET",
    "path": "/books/{book_id}/prefs",
    "tag": "prefs",
    "auth": true,
    "summary": "The directing style learned for one book.",
    "requestModel": null,
    "responseModel": "DirectingStyleResponse",
    "status": 200
  },
  {
    "id": "resetMyPrefs",
    "method": "DELETE",
    "path": "/me/prefs",
    "tag": "prefs",
    "auth": true,
    "summary": "Clear the reader's learned directing style everywhere.",
    "requestModel": null,
    "responseModel": "ResetPrefsResponse",
    "status": 200
  },
  {
    "id": "resetBookPrefs",
    "method": "DELETE",
    "path": "/books/{book_id}/prefs",
    "tag": "prefs",
    "auth": true,
    "summary": "Clear the directing style learned for one book.",
    "requestModel": null,
    "responseModel": "ResetPrefsResponse",
    "status": 200
  },
  {
    "id": "getBufferTrace",
    "method": "GET",
    "path": "/eval/buffer-trace/{session_id}",
    "tag": "eval",
    "auth": true,
    "summary": "Recompute the watermark buffer sawtooth for a session (zero video-seconds).",
    "requestModel": null,
    "responseModel": "BufferTracePoint[]",
    "status": 200,
    "query": {
      "velocity": "Override reading velocity in words/sec (0 < v <= 40).",
      "duration_s": "Simulation horizon in seconds (10 <= d <= 1200)."
    }
  },
  {
    "id": "getEvalReport",
    "method": "GET",
    "path": "/eval/report/{book_id}",
    "tag": "eval",
    "auth": true,
    "summary": "The cached crew-vs-baseline evaluation report for a book.",
    "requestModel": null,
    "responseModel": "EvalReport",
    "status": 200
  },
  {
    "id": "getCost",
    "method": "GET",
    "path": "/optim/cost",
    "tag": "optim",
    "auth": true,
    "summary": "Per book / session / model / operation USD rollup.",
    "requestModel": null,
    "responseModel": "CostReport",
    "status": 200
  },
  {
    "id": "getPerf",
    "method": "GET",
    "path": "/optim/perf",
    "tag": "optim",
    "auth": true,
    "summary": "Compact cost/uptime summary for an in-app HUD.",
    "requestModel": null,
    "responseModel": "PerfReport",
    "status": 200
  },
  {
    "id": "sessionEvents",
    "method": "GET",
    "path": "/sessions/{session_id}/events",
    "tag": "events",
    "auth": true,
    "summary": "SSE stream of a session's generation events.",
    "requestModel": null,
    "responseModel": "text/event-stream",
    "status": 200,
    "query": {
      "token": "Bearer token (SSE cannot set headers; pass it as a query param)."
    }
  },
  {
    "id": "libraryEvents",
    "method": "GET",
    "path": "/books/events",
    "tag": "events",
    "auth": true,
    "summary": "SSE stream of ingest progress for the signed-in user's library.",
    "requestModel": null,
    "responseModel": "text/event-stream",
    "status": 200,
    "query": {
      "token": "Bearer token (SSE cannot set headers; pass it as a query param)."
    }
  }
] as const;

export const EVENTS: readonly EventSpec[] = [
  {
    "name": "buffer_state",
    "summary": "Live committed-buffer state from one Scheduler control tick.",
    "fields": [
      "committed_seconds_ahead",
      "bursting",
      "idle",
      "velocity_wps",
      "budget_remaining_s"
    ],
    "channels": [
      "session"
    ]
  },
  {
    "name": "clip_ready",
    "summary": "A shot's clip finished rendering and is playable.",
    "fields": [
      "shot_id",
      "oss_url",
      "video_seconds"
    ],
    "channels": [
      "session"
    ]
  },
  {
    "name": "keyframe_ready",
    "summary": "A speculative keyframe still was generated for a beat.",
    "fields": [
      "shot_id",
      "beat_id",
      "oss_url"
    ],
    "channels": [
      "session"
    ]
  },
  {
    "name": "scene_stitched",
    "summary": "A scene's accepted shots were stitched into one continuous film.",
    "fields": [
      "scene_id",
      "oss_url",
      "sync_map"
    ],
    "channels": [
      "session",
      "book"
    ]
  },
  {
    "name": "event_stitched",
    "summary": "An event-level film rollup (event == scene today).",
    "fields": [
      "event_id",
      "oss_url",
      "sync_map"
    ],
    "channels": [
      "session",
      "book"
    ]
  },
  {
    "name": "agent_activity",
    "summary": "A crew agent did something visible (routing, canon write, arbitration).",
    "fields": [
      "agent",
      "aspect",
      "message",
      "shot_id",
      "job_id",
      "conflict"
    ],
    "channels": [
      "session",
      "book"
    ]
  },
  {
    "name": "regen_done",
    "summary": "A targeted regeneration completed; the fresh clip closes the loop.",
    "fields": [
      "shot_id",
      "oss_url",
      "qa"
    ],
    "channels": [
      "session",
      "book"
    ]
  },
  {
    "name": "budget_low",
    "summary": "The video-second budget is running low.",
    "fields": [
      "budget_remaining_s",
      "scope"
    ],
    "channels": [
      "session"
    ]
  },
  {
    "name": "conflict_choice",
    "summary": "A continuity conflict was surfaced for the Director to resolve.",
    "fields": [
      "conflict_id",
      "options",
      "claim",
      "canon_fact",
      "current_beat",
      "raised_by",
      "shot_id"
    ],
    "channels": [
      "session"
    ]
  },
  {
    "name": "ingest_progress",
    "summary": "Phase-A ingest progress for a book on the user's library channel.",
    "fields": [
      "book_id",
      "stage",
      "pct"
    ],
    "channels": [
      "library",
      "book"
    ]
  }
] as const;

export const ERROR_TYPES: readonly ErrorTypeSpec[] = [
  {
    "type": "validation_error",
    "status": 422,
    "summary": "Request body/params failed validation."
  },
  {
    "type": "invalid_credentials",
    "status": 401,
    "summary": "Wrong email or password."
  },
  {
    "type": "unauthorized",
    "status": 401,
    "summary": "Missing or invalid bearer token."
  },
  {
    "type": "email_taken",
    "status": 409,
    "summary": "An account with this email already exists."
  },
  {
    "type": "book_not_found",
    "status": 404,
    "summary": "No such book for this user."
  },
  {
    "type": "page_not_found",
    "status": 404,
    "summary": "No such page."
  },
  {
    "type": "session_not_found",
    "status": 404,
    "summary": "No such session for this user."
  },
  {
    "type": "shot_not_found",
    "status": 404,
    "summary": "No such shot in this session's book."
  },
  {
    "type": "entity_not_found",
    "status": 404,
    "summary": "No such canon entity."
  },
  {
    "type": "scene_not_found",
    "status": 404,
    "summary": "No such scene."
  },
  {
    "type": "book_quota_exceeded",
    "status": 429,
    "summary": "Per-user book limit reached."
  },
  {
    "type": "file_too_large",
    "status": 413,
    "summary": "Upload exceeds the size limit."
  },
  {
    "type": "too_many_pages",
    "status": 413,
    "summary": "Document exceeds the per-book page limit."
  },
  {
    "type": "unsupported_media_type",
    "status": 415,
    "summary": "Expected a PDF or EPUB upload."
  },
  {
    "type": "budget_exceeded",
    "status": 402,
    "summary": "Video budget cap reached."
  },
  {
    "type": "live_video_disabled",
    "status": 409,
    "summary": "Live video generation is gated off."
  },
  {
    "type": "provider_error",
    "status": 502,
    "summary": "An upstream model failure."
  },
  {
    "type": "forbidden",
    "status": 403,
    "summary": "Action not permitted in this environment."
  },
  {
    "type": "internal_error",
    "status": 500,
    "summary": "Unexpected server error (scrubbed in prod)."
  }
] as const;

export const CONFLICT_OPTIONS: readonly string[] = [
  "honor_canon",
  "evolve_canon",
  "surface_to_user"
] as const;

export const WEBSOCKET = {
  "path": "/ws/sessions/{session_id}",
  "summary": "Bidirectional Director channel: fans out the same SSE events and accepts client->backend messages (intent_update, seek, comment).",
  "clientMessages": [
    "intent_update",
    "seek",
    "comment"
  ],
  "auth": "Bearer header or ?token= query parameter."
} as const;

export const MODELS: Readonly<Record<string, Readonly<Record<string, string>>>> = {
  "RegisterRequest": {
    "email": "string",
    "password": "string"
  },
  "LoginRequest": {
    "email": "string",
    "password": "string"
  },
  "TokenResponse": {
    "access_token": "string",
    "token_type": "string",
    "expires_in": "integer"
  },
  "UserResponse": {
    "id": "string",
    "email": "string",
    "created_at": "string?"
  },
  "BookResponse": {
    "id": "string",
    "title": "string",
    "author": "string?",
    "status": "string",
    "num_pages": "integer?",
    "art_direction": "string?",
    "created_at": "string?",
    "progress": "number?",
    "stage": "string?",
    "cover_url": "string?"
  },
  "PageResponse": {
    "book_id": "string",
    "page_number": "integer",
    "image_url": "string?",
    "text": "string?",
    "word_boxes": "object[]"
  },
  "CanonReferenceImage": {
    "oss_url": "string",
    "oss_key": "string?",
    "pose": "string?",
    "locked": "boolean?"
  },
  "CanonAppearance": {
    "description": "string?",
    "reference_images": "CanonReferenceImage[]"
  },
  "CanonEntityResponse": {
    "id": "string",
    "type": "string",
    "name": "string",
    "aliases": "string[]",
    "description": "string?",
    "appearance": "CanonAppearance?",
    "style_tokens": "object?",
    "voice": "object?",
    "version": "integer",
    "valid_from_beat": "integer?",
    "valid_to_beat": "integer?",
    "first_appearance": "object?"
  },
  "CanonStateResponse": {
    "id": "string",
    "subject_entity_key": "string",
    "predicate": "string",
    "object_value": "string",
    "valid_from_beat": "integer",
    "valid_to_beat": "integer?",
    "version": "integer",
    "active": "boolean",
    "source_span": "object?"
  },
  "CanonResponse": {
    "book_id": "string",
    "entities": "CanonEntityResponse[]",
    "states": "CanonStateResponse[]",
    "markdown": "string?"
  },
  "ShotResponse": {
    "shot_id": "string",
    "beat_id": "string?",
    "scene_id": "string?",
    "source_span": "object?",
    "status": "string",
    "render_mode": "string?",
    "duration_s": "number?",
    "qa": "object?",
    "clip_url": "string?",
    "reference_image_ids": "string[]"
  },
  "CreateSessionRequest": {
    "book_id": "string",
    "focus_word": "integer",
    "mode": "string"
  },
  "SessionResponse": {
    "session_id": "string",
    "book_id": "string",
    "focus_word": "integer",
    "velocity_wps": "number",
    "mode": "string",
    "committed_seconds_ahead": "number",
    "bursting": "boolean",
    "budget_remaining_s": "number?",
    "inflight": "object"
  },
  "IntentRequest": {
    "focus_word": "integer",
    "velocity": "number",
    "mode": "string?"
  },
  "IntentResponse": {
    "session_id": "string",
    "settled": "boolean",
    "allow_promotion": "boolean",
    "idle": "boolean",
    "bursting": "boolean",
    "committed_seconds_ahead": "number",
    "promoted": "string[]",
    "keyframed": "string[]",
    "cancelled": "integer"
  },
  "SeekRequest": {
    "word": "integer"
  },
  "SeekResponse": {
    "session_id": "string",
    "word": "integer",
    "cancelled": "integer",
    "bridge_beat": "string?",
    "committed_seconds_ahead": "number"
  },
  "CommentRequest": {
    "shot_id": "string",
    "note": "string",
    "region_png": "string?"
  },
  "CommentResponse": {
    "shot_id": "string",
    "agent": "string",
    "aspect": "string",
    "message": "string",
    "job_id": "string?",
    "learned": "DirectingPriorView[]"
  },
  "CanonEditRequest": {
    "entity_key": "string",
    "changes": "object",
    "valid_from_beat": "integer?"
  },
  "CanonEditResponse": {
    "entity_key": "string",
    "version": "integer",
    "affected_shot_ids": "string[]",
    "skipped_shots": "integer"
  },
  "ConflictChoiceRequest": {
    "conflict_id": "string",
    "option": "string"
  },
  "ConflictChoiceResponse": {
    "conflict_id": "string",
    "option": "string",
    "status": "string",
    "shot_id": "string?",
    "reasoning": "string?"
  },
  "ConflictRecordResponse": {
    "conflict_id": "string",
    "shot_id": "string?",
    "claim": "string?",
    "canon_fact": "string?",
    "raised_by": "string?",
    "current_beat": "string?",
    "options": "object[]",
    "resolved": "boolean",
    "chosen_option": "string?",
    "reasoning": "string?"
  },
  "DirectingPriorView": {
    "kind": "string",
    "bias": "number",
    "weight": "number",
    "label": "string",
    "detail": "string",
    "applied": "boolean",
    "applied_value": "string?",
    "last_note": "string?"
  },
  "DirectingStyleResponse": {
    "scope": "string",
    "book_id": "string?",
    "priors": "DirectingPriorView[]"
  },
  "ResetPrefsResponse": {
    "scope": "string",
    "book_id": "string?",
    "cleared": "integer"
  },
  "SyncWord": {
    "word_index": "integer",
    "text": "string",
    "t_start": "number",
    "t_end": "number",
    "bbox": "number[]?"
  },
  "FilmSyncSegment": {
    "shot_id": "string",
    "scene_id": "string",
    "word_range": "integer[]",
    "t_start_s": "number",
    "t_end_s": "number",
    "page": "integer",
    "page_turn_at_s": "number",
    "words": "SyncWord[]"
  },
  "FilmSyncMap": {
    "scene_id": "string",
    "duration_s": "number",
    "segments": "FilmSyncSegment[]"
  },
  "SceneRef": {
    "scene_id": "string",
    "scene_index": "integer",
    "word_range": "integer[]",
    "stitched": "boolean",
    "duration_s": "number?"
  },
  "SceneFilm": {
    "scene_id": "string",
    "event_id": "string",
    "book_id": "string",
    "scene_index": "integer",
    "event_index": "integer",
    "page_start": "integer",
    "page_end": "integer",
    "word_range": "integer[]",
    "stitched": "boolean",
    "oss_url": "string?",
    "url_expires_at": "string?",
    "duration_s": "number?",
    "shot_count": "integer",
    "sync_map": "FilmSyncMap"
  },
  "EventFilm": {
    "event_id": "string",
    "event_index": "integer",
    "book_id": "string",
    "page_start": "integer",
    "page_end": "integer",
    "word_range": "integer[]",
    "stitched": "boolean",
    "oss_url": "string?",
    "url_expires_at": "string?",
    "duration_s": "number?",
    "shot_count": "integer",
    "sync_map": "FilmSyncMap",
    "scenes": "SceneRef[]"
  },
  "RestoreState": {
    "session_id": "string",
    "focus_word": "integer",
    "current_event_index": "integer?",
    "current_scene_id": "string?",
    "mode": "string"
  },
  "EventsResponse": {
    "book_id": "string",
    "url_ttl_s": "integer",
    "events": "EventFilm[]",
    "restore": "RestoreState?"
  },
  "BufferTracePoint": {
    "t": "number",
    "committed_seconds_ahead": "number",
    "low": "number",
    "high": "number"
  },
  "EvalReport": {
    "ccs": "object",
    "efficiency": "object",
    "regen_rate": "object",
    "style_drift": "object",
    "runs": "integer",
    "thresholds": "object",
    "per_character_ccs": "object"
  },
  "CostReport": {
    "priced_models": "object[]",
    "rollup": "object"
  },
  "PerfReport": {
    "uptime_s": "number",
    "priced_model_count": "integer",
    "totals": "object",
    "by_operation": "object"
  },
  "ErrorBody": {
    "type": "string",
    "message": "string",
    "detail": "object?"
  },
  "ErrorResponse": {
    "error": "ErrorBody"
  }
};

/** Build the full path for an endpoint (prepends API_PREFIX). */
export function fullPath(e: EndpointSpec): string {
  return `${API_PREFIX}${e.path}`;
}

/** Endpoints grouped by tag, preserving declaration order. */
export function endpointsByTag(): Map<string, EndpointSpec[]> {
  const out = new Map<string, EndpointSpec[]>();
  for (const e of ENDPOINTS) {
    const list = out.get(e.tag) ?? [];
    list.push(e);
    out.set(e.tag, list);
  }
  return out;
}
