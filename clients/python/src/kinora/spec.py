"""GENERATED FILE — do not edit by hand.

Emitted from clients/spec/catalog.mjs by `node clients/spec/sync-py.mjs`. The
catalog is the single source of truth for the Kinora API surface; this module
gives the Python SDK + the contract-drift test a typed view of it.
"""

from __future__ import annotations

from typing import Any, TypedDict


class EndpointSpec(TypedDict, total=False):
    id: str
    method: str
    path: str
    tag: str
    auth: bool
    summary: str
    requestModel: str | None
    responseModel: str | None
    status: int
    query: dict[str, str]


class EventSpec(TypedDict):
    name: str
    summary: str
    fields: list[str]
    channels: list[str]


class ErrorTypeSpec(TypedDict):
    type: str
    status: int
    summary: str


API_VERSION: str = "1.0.0"
API_PREFIX: str = "/api"
DEFAULT_BASE_URL: str = "http://localhost:8000"

ENDPOINTS: list[EndpointSpec] = [
    {
        "id": "register",
        "method": "POST",
        "path": "/auth/register",
        "tag": "auth",
        "auth": False,
        "summary": "Create an account (email + password >= 8 chars).",
        "requestModel": "RegisterRequest",
        "responseModel": "UserResponse",
        "status": 201,
    },
    {
        "id": "login",
        "method": "POST",
        "path": "/auth/login",
        "tag": "auth",
        "auth": False,
        "summary": "Exchange credentials for a bearer access token.",
        "requestModel": "LoginRequest",
        "responseModel": "TokenResponse",
        "status": 200,
    },
    {
        "id": "me",
        "method": "GET",
        "path": "/auth/me",
        "tag": "auth",
        "auth": True,
        "summary": "Return the authenticated user.",
        "requestModel": None,
        "responseModel": "UserResponse",
        "status": 200,
    },
    {
        "id": "uploadBook",
        "method": "POST",
        "path": "/books",
        "tag": "books",
        "auth": True,
        "summary": "Upload a PDF/EPUB and trigger Phase-A ingest. Multipart form.",
        "requestModel": "multipart",
        "responseModel": "BookResponse",
        "status": 201,
    },
    {
        "id": "listBooks",
        "method": "GET",
        "path": "/books",
        "tag": "books",
        "auth": True,
        "summary": "List the books the current user owns (the shelf), newest first.",
        "requestModel": None,
        "responseModel": "BookResponse[]",
        "status": 200,
    },
    {
        "id": "getBook",
        "method": "GET",
        "path": "/books/{book_id}",
        "tag": "books",
        "auth": True,
        "summary": "Fetch one book with its import status + progress.",
        "requestModel": None,
        "responseModel": "BookResponse",
        "status": 200,
    },
    {
        "id": "getPage",
        "method": "GET",
        "path": "/books/{book_id}/pages/{page_number}",
        "tag": "books",
        "auth": True,
        "summary": "A page's presigned image URL, text, and per-word boxes.",
        "requestModel": None,
        "responseModel": "PageResponse",
        "status": 200,
    },
    {
        "id": "getCanon",
        "method": "GET",
        "path": "/books/{book_id}/canon",
        "tag": "books",
        "auth": True,
        "summary": "The book's canon graph: entities, continuity facts, markdown vault.",
        "requestModel": None,
        "responseModel": "CanonResponse",
        "status": 200,
    },
    {
        "id": "listShots",
        "method": "GET",
        "path": "/books/{book_id}/shots",
        "tag": "books",
        "auth": True,
        "summary": "The book's shots (the shot timeline) as a bare array.",
        "requestModel": None,
        "responseModel": "ShotResponse[]",
        "status": 200,
    },
    {
        "id": "getBookCover",
        "method": "GET",
        "path": "/books/{book_id}/cover",
        "tag": "books",
        "auth": True,
        "summary": "302-redirect to the presigned cover image for an owned book.",
        "requestModel": None,
        "responseModel": None,
        "status": 302,
    },
    {
        "id": "listEvents",
        "method": "GET",
        "path": "/books/{book_id}/events",
        "tag": "films",
        "auth": True,
        "summary": "Every event (scene) film — stitched URL + sync map + restore state.",
        "requestModel": None,
        "responseModel": "EventsResponse",
        "status": 200,
    },
    {
        "id": "getSceneFilm",
        "method": "GET",
        "path": "/books/{book_id}/scenes/{scene_id}/film",
        "tag": "films",
        "auth": True,
        "summary": "One scene's film (partial load).",
        "requestModel": None,
        "responseModel": "SceneFilm",
        "status": 200,
    },
    {
        "id": "createSession",
        "method": "POST",
        "path": "/sessions",
        "tag": "sessions",
        "auth": True,
        "summary": "Open a reading session against a book.",
        "requestModel": "CreateSessionRequest",
        "responseModel": "SessionResponse",
        "status": 201,
    },
    {
        "id": "getSession",
        "method": "GET",
        "path": "/sessions/{session_id}",
        "tag": "sessions",
        "auth": True,
        "summary": "Return the Scheduler's live control state for a session.",
        "requestModel": None,
        "responseModel": "SessionResponse",
        "status": 200,
    },
    {
        "id": "updateIntent",
        "method": "POST",
        "path": "/sessions/{session_id}/intent",
        "tag": "sessions",
        "auth": True,
        "summary": "Apply a debounced reading-intent update and run one control tick.",
        "requestModel": "IntentRequest",
        "responseModel": "IntentResponse",
        "status": 200,
    },
    {
        "id": "seek",
        "method": "POST",
        "path": "/sessions/{session_id}/seek",
        "tag": "sessions",
        "auth": True,
        "summary": "Jump to a word: cancel distant work, bridge keyframe, re-seed.",
        "requestModel": "SeekRequest",
        "responseModel": "SeekResponse",
        "status": 200,
    },
    {
        "id": "comment",
        "method": "POST",
        "path": "/sessions/{session_id}/comment",
        "tag": "director",
        "auth": True,
        "summary": "Classify a Director region-comment, enqueue a regen, emit agent_activity.",
        "requestModel": "CommentRequest",
        "responseModel": "CommentResponse",
        "status": 200,
    },
    {
        "id": "canonEdit",
        "method": "POST",
        "path": "/books/{book_id}/canon_edit",
        "tag": "director",
        "auth": True,
        "summary": "Edit a canon entity and surgically regen only the dependent shots.",
        "requestModel": "CanonEditRequest",
        "responseModel": "CanonEditResponse",
        "status": 200,
    },
    {
        "id": "conflictChoice",
        "method": "POST",
        "path": "/sessions/{session_id}/conflict_choice",
        "tag": "director",
        "auth": True,
        "summary": "Apply the Director's resolution of a surfaced continuity conflict.",
        "requestModel": "ConflictChoiceRequest",
        "responseModel": "ConflictChoiceResponse",
        "status": 200,
    },
    {
        "id": "listConflicts",
        "method": "GET",
        "path": "/sessions/{session_id}/conflicts",
        "tag": "director",
        "auth": True,
        "summary": "The session's conflict log — surfaced disputes + their resolutions.",
        "requestModel": None,
        "responseModel": "ConflictRecordResponse[]",
        "status": 200,
    },
    {
        "id": "demoConflict",
        "method": "POST",
        "path": "/sessions/{session_id}/demo/conflict",
        "tag": "director",
        "auth": True,
        "summary": "DEV-ONLY: surface the canonical lost-sword conflict (local env only).",
        "requestModel": None,
        "responseModel": "ConflictRecordResponse",
        "status": 200,
    },
    {
        "id": "getMyPrefs",
        "method": "GET",
        "path": "/me/prefs",
        "tag": "prefs",
        "auth": True,
        "summary": "The reader's accumulated directing style across all their books.",
        "requestModel": None,
        "responseModel": "DirectingStyleResponse",
        "status": 200,
    },
    {
        "id": "getBookPrefs",
        "method": "GET",
        "path": "/books/{book_id}/prefs",
        "tag": "prefs",
        "auth": True,
        "summary": "The directing style learned for one book.",
        "requestModel": None,
        "responseModel": "DirectingStyleResponse",
        "status": 200,
    },
    {
        "id": "resetMyPrefs",
        "method": "DELETE",
        "path": "/me/prefs",
        "tag": "prefs",
        "auth": True,
        "summary": "Clear the reader's learned directing style everywhere.",
        "requestModel": None,
        "responseModel": "ResetPrefsResponse",
        "status": 200,
    },
    {
        "id": "resetBookPrefs",
        "method": "DELETE",
        "path": "/books/{book_id}/prefs",
        "tag": "prefs",
        "auth": True,
        "summary": "Clear the directing style learned for one book.",
        "requestModel": None,
        "responseModel": "ResetPrefsResponse",
        "status": 200,
    },
    {
        "id": "getBufferTrace",
        "method": "GET",
        "path": "/eval/buffer-trace/{session_id}",
        "tag": "eval",
        "auth": True,
        "summary": "Recompute the watermark buffer sawtooth for a session (zero video-seconds).",
        "requestModel": None,
        "responseModel": "BufferTracePoint[]",
        "status": 200,
        "query": {
            "velocity": "Override reading velocity in words/sec (0 < v <= 40).",
            "duration_s": "Simulation horizon in seconds (10 <= d <= 1200).",
        },
    },
    {
        "id": "getEvalReport",
        "method": "GET",
        "path": "/eval/report/{book_id}",
        "tag": "eval",
        "auth": True,
        "summary": "The cached crew-vs-baseline evaluation report for a book.",
        "requestModel": None,
        "responseModel": "EvalReport",
        "status": 200,
    },
    {
        "id": "getCost",
        "method": "GET",
        "path": "/optim/cost",
        "tag": "optim",
        "auth": True,
        "summary": "Per book / session / model / operation USD rollup.",
        "requestModel": None,
        "responseModel": "CostReport",
        "status": 200,
    },
    {
        "id": "getPerf",
        "method": "GET",
        "path": "/optim/perf",
        "tag": "optim",
        "auth": True,
        "summary": "Compact cost/uptime summary for an in-app HUD.",
        "requestModel": None,
        "responseModel": "PerfReport",
        "status": 200,
    },
    {
        "id": "sessionEvents",
        "method": "GET",
        "path": "/sessions/{session_id}/events",
        "tag": "events",
        "auth": True,
        "summary": "SSE stream of a session's generation events.",
        "requestModel": None,
        "responseModel": "text/event-stream",
        "status": 200,
        "query": {
            "token": "Bearer token (SSE cannot set headers; pass it as a query param).",
        },
    },
    {
        "id": "libraryEvents",
        "method": "GET",
        "path": "/books/events",
        "tag": "events",
        "auth": True,
        "summary": "SSE stream of ingest progress for the signed-in user's library.",
        "requestModel": None,
        "responseModel": "text/event-stream",
        "status": 200,
        "query": {
            "token": "Bearer token (SSE cannot set headers; pass it as a query param).",
        },
    },
]

EVENTS: list[EventSpec] = [
    {
        "name": "buffer_state",
        "summary": "Live committed-buffer state from one Scheduler control tick.",
        "fields": [
            "committed_seconds_ahead",
            "bursting",
            "idle",
            "velocity_wps",
            "budget_remaining_s",
        ],
        "channels": [
            "session",
        ],
    },
    {
        "name": "clip_ready",
        "summary": "A shot's clip finished rendering and is playable.",
        "fields": [
            "shot_id",
            "oss_url",
            "video_seconds",
        ],
        "channels": [
            "session",
        ],
    },
    {
        "name": "keyframe_ready",
        "summary": "A speculative keyframe still was generated for a beat.",
        "fields": [
            "shot_id",
            "beat_id",
            "oss_url",
        ],
        "channels": [
            "session",
        ],
    },
    {
        "name": "scene_stitched",
        "summary": "A scene's accepted shots were stitched into one continuous film.",
        "fields": [
            "scene_id",
            "oss_url",
            "sync_map",
        ],
        "channels": [
            "session",
            "book",
        ],
    },
    {
        "name": "event_stitched",
        "summary": "An event-level film rollup (event == scene today).",
        "fields": [
            "event_id",
            "oss_url",
            "sync_map",
        ],
        "channels": [
            "session",
            "book",
        ],
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
            "conflict",
        ],
        "channels": [
            "session",
            "book",
        ],
    },
    {
        "name": "regen_done",
        "summary": "A targeted regeneration completed; the fresh clip closes the loop.",
        "fields": [
            "shot_id",
            "oss_url",
            "qa",
        ],
        "channels": [
            "session",
            "book",
        ],
    },
    {
        "name": "budget_low",
        "summary": "The video-second budget is running low.",
        "fields": [
            "budget_remaining_s",
            "scope",
        ],
        "channels": [
            "session",
        ],
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
            "shot_id",
        ],
        "channels": [
            "session",
        ],
    },
    {
        "name": "ingest_progress",
        "summary": "Phase-A ingest progress for a book on the user's library channel.",
        "fields": [
            "book_id",
            "stage",
            "pct",
        ],
        "channels": [
            "library",
            "book",
        ],
    },
]

ERROR_TYPES: list[ErrorTypeSpec] = [
    {
        "type": "validation_error",
        "status": 422,
        "summary": "Request body/params failed validation.",
    },
    {
        "type": "invalid_credentials",
        "status": 401,
        "summary": "Wrong email or password.",
    },
    {
        "type": "unauthorized",
        "status": 401,
        "summary": "Missing or invalid bearer token.",
    },
    {
        "type": "email_taken",
        "status": 409,
        "summary": "An account with this email already exists.",
    },
    {
        "type": "book_not_found",
        "status": 404,
        "summary": "No such book for this user.",
    },
    {
        "type": "page_not_found",
        "status": 404,
        "summary": "No such page.",
    },
    {
        "type": "session_not_found",
        "status": 404,
        "summary": "No such session for this user.",
    },
    {
        "type": "shot_not_found",
        "status": 404,
        "summary": "No such shot in this session's book.",
    },
    {
        "type": "entity_not_found",
        "status": 404,
        "summary": "No such canon entity.",
    },
    {
        "type": "scene_not_found",
        "status": 404,
        "summary": "No such scene.",
    },
    {
        "type": "book_quota_exceeded",
        "status": 429,
        "summary": "Per-user book limit reached.",
    },
    {
        "type": "file_too_large",
        "status": 413,
        "summary": "Upload exceeds the size limit.",
    },
    {
        "type": "too_many_pages",
        "status": 413,
        "summary": "Document exceeds the per-book page limit.",
    },
    {
        "type": "unsupported_media_type",
        "status": 415,
        "summary": "Expected a PDF or EPUB upload.",
    },
    {
        "type": "budget_exceeded",
        "status": 402,
        "summary": "Video budget cap reached.",
    },
    {
        "type": "live_video_disabled",
        "status": 409,
        "summary": "Live video generation is gated off.",
    },
    {
        "type": "provider_error",
        "status": 502,
        "summary": "An upstream model failure.",
    },
    {
        "type": "forbidden",
        "status": 403,
        "summary": "Action not permitted in this environment.",
    },
    {
        "type": "internal_error",
        "status": 500,
        "summary": "Unexpected server error (scrubbed in prod).",
    },
]

CONFLICT_OPTIONS: list[str] = [
    "honor_canon",
    "evolve_canon",
    "surface_to_user",
]

WEBSOCKET: dict[str, Any] = {
    "path": "/ws/sessions/{session_id}",
    "summary": "Bidirectional Director channel: fans out the same SSE events and accepts client->backend messages (intent_update, seek, comment).",
    "clientMessages": [
        "intent_update",
        "seek",
        "comment",
    ],
    "auth": "Bearer header or ?token= query parameter.",
}


def full_path(endpoint: EndpointSpec) -> str:
    """Build the full path for an endpoint (prepends API_PREFIX)."""
    return f"{API_PREFIX}{endpoint['path']}"


def endpoints_by_tag() -> dict[str, list[EndpointSpec]]:
    """Endpoints grouped by tag, preserving declaration order."""
    out: dict[str, list[EndpointSpec]] = {}
    for endpoint in ENDPOINTS:
        out.setdefault(endpoint["tag"], []).append(endpoint)
    return out


__all__ = [
    "API_VERSION",
    "API_PREFIX",
    "DEFAULT_BASE_URL",
    "ENDPOINTS",
    "EVENTS",
    "ERROR_TYPES",
    "CONFLICT_OPTIONS",
    "WEBSOCKET",
    "EndpointSpec",
    "EventSpec",
    "ErrorTypeSpec",
    "full_path",
    "endpoints_by_tag",
]
