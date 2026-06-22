"""Request/response DTOs for the API gateway (kinora.md §5.6).

These are the *transport* contracts (what crosses the wire), distinct from the
internal memory/agent contracts. Inputs validate untrusted client data; outputs
project DB rows / service results into stable JSON shapes. Email is a plain
string with a light shape check so the gateway needs no extra dependency.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


class RegisterRequest(BaseModel):
    """Create an account."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or "." not in value.split("@")[-1]:
            raise ValueError("invalid email address")
        return value


class LoginRequest(BaseModel):
    """Exchange credentials for an access token."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class TokenResponse(BaseModel):
    """A freshly-issued access token."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    """A public view of a user account."""

    id: str
    email: str
    created_at: str | None = None


# --------------------------------------------------------------------------- #
# Books
# --------------------------------------------------------------------------- #


class BookResponse(BaseModel):
    """A book on the shelf, with its import status and progress (§5.1).

    The collection endpoints (``GET /books``) return a *bare* ``list`` of these,
    and the upload endpoint returns a bare ``BookResponse`` — the frontend client
    parses ``Book[]`` / ``Book`` directly (no envelope).
    """

    id: str
    title: str
    author: str | None = None
    status: str
    num_pages: int | None = None
    art_direction: str | None = None
    created_at: str | None = None
    progress: float | None = None
    #: Current ingest stage label (the frontend reads ``book.stage``).
    stage: str | None = None


class PageResponse(BaseModel):
    """One rendered page: a presigned image URL, text, and per-word boxes (§9.4)."""

    book_id: str
    page_number: int
    image_url: str | None = None
    text: str | None = None
    word_boxes: list[dict[str, Any]] = Field(default_factory=list)


class CanonReferenceImage(BaseModel):
    """A locked reference image projected for the Director's canon editor."""

    oss_url: str
    pose: str | None = None
    locked: bool | None = None


class CanonAppearance(BaseModel):
    """An entity's appearance block with presigned reference-image URLs."""

    description: str | None = None
    reference_images: list[CanonReferenceImage] = Field(default_factory=list)


class CanonEntityResponse(BaseModel):
    """One canon entity (current version) projected for the §5.4 canon editor.

    ``id`` is the stable ``entity_key`` (what the canon-edit call targets), not
    the per-version DB row id.
    """

    id: str
    type: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    appearance: CanonAppearance | None = None
    style_tokens: dict[str, Any] | None = None
    voice: dict[str, Any] | None = None
    version: int
    valid_from_beat: int | None = None
    valid_to_beat: int | None = None
    first_appearance: dict[str, Any] | None = None


class CanonResponse(BaseModel):
    """The canon graph for a book: the entity list the Director editor renders,
    plus the optional human-inspectable markdown vault export (§8.1)."""

    book_id: str
    entities: list[CanonEntityResponse] = Field(default_factory=list)
    markdown: str | None = None


class ShotResponse(BaseModel):
    """A shot's episodic record projected for the timeline / Director tools.

    Includes ``source_span`` so the client's SyncEngine can sort/seek by reading
    position; the §5.4 timeline returns a *bare* ``list`` of these."""

    shot_id: str
    beat_id: str | None = None
    scene_id: str | None = None
    source_span: dict[str, Any] | None = None
    status: str
    render_mode: str | None = None
    duration_s: float | None = None
    qa: dict[str, Any] | None = None
    clip_url: str | None = None
    reference_image_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Sessions / intent / seek
# --------------------------------------------------------------------------- #


class CreateSessionRequest(BaseModel):
    """Open a reading session against a book."""

    model_config = ConfigDict(extra="forbid")

    book_id: str
    focus_word: int = Field(default=0, ge=0)
    mode: str = "viewer"


class SessionResponse(BaseModel):
    """The Scheduler's view of a reading session (§4.9)."""

    session_id: str
    book_id: str
    focus_word: int
    velocity_wps: float
    mode: str
    committed_seconds_ahead: float
    bursting: bool = False
    budget_remaining_s: float | None = None
    inflight: dict[str, list[str]] = Field(default_factory=dict)


class IntentRequest(BaseModel):
    """A debounced reading-intent update (§4.3): focus word ``w`` + velocity ``v``."""

    model_config = ConfigDict(extra="forbid")

    focus_word: int = Field(ge=0)
    velocity: float = 4.0
    mode: str | None = None


class IntentResponse(BaseModel):
    """What one control tick did (§4.9): promotions, keyframes, buffer state."""

    session_id: str
    settled: bool
    allow_promotion: bool = False
    idle: bool = False
    bursting: bool = False
    committed_seconds_ahead: float = 0.0
    promoted: list[str] = Field(default_factory=list)
    keyframed: list[str] = Field(default_factory=list)
    cancelled: int = 0


class SeekRequest(BaseModel):
    """A jump to a word (§4.8): cancel distant work, bridge, re-seed."""

    model_config = ConfigDict(extra="forbid")

    word: int = Field(ge=0)


class SeekResponse(BaseModel):
    """The outcome of a seek (§4.8)."""

    session_id: str
    word: int
    cancelled: int
    bridge_beat: str | None = None
    committed_seconds_ahead: float = 0.0


# --------------------------------------------------------------------------- #
# Director tools (§5.4)
# --------------------------------------------------------------------------- #


class CommentRequest(BaseModel):
    """A Director region-comment: a screenshot + a natural-language note (§5.4)."""

    model_config = ConfigDict(extra="forbid")

    shot_id: str
    note: str = Field(min_length=1, max_length=2000)
    region_png: str | None = None  # base64-encoded PNG of the selected region


class CommentResponse(BaseModel):
    """How a comment was routed and the regen it triggered (§5.4)."""

    shot_id: str
    agent: str
    aspect: str
    message: str
    job_id: str | None = None


class CanonEditRequest(BaseModel):
    """An edit to a canon entity, triggering surgical dependent regen (§5.4/§8.7)."""

    model_config = ConfigDict(extra="forbid")

    entity_key: str
    changes: dict[str, Any] = Field(default_factory=dict)
    valid_from_beat: int | None = Field(default=None, ge=0)


class CanonEditResponse(BaseModel):
    """The new entity version + the dependent shots queued for regen (§8.7).

    ``affected_shot_ids`` are the dependent shots the edit re-rendered — the name
    the frontend's canon editor reads to mark those shots "rendering"."""

    entity_key: str
    version: int
    affected_shot_ids: list[str] = Field(default_factory=list)
    skipped_shots: int = 0


class ConflictChoiceRequest(BaseModel):
    """The Director's resolution of a surfaced conflict (§7.2)."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    option: str


class ConflictChoiceResponse(BaseModel):
    """Acknowledgement that a conflict choice was recorded (§7.2)."""

    conflict_id: str
    option: str
    status: str = "recorded"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ErrorBody(BaseModel):
    """A typed error payload (never leaks secrets/stack traces, §12)."""

    type: str
    message: str
    detail: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """The envelope every error response uses."""

    error: ErrorBody


__all__ = [
    "BookResponse",
    "CanonAppearance",
    "CanonEditRequest",
    "CanonEditResponse",
    "CanonEntityResponse",
    "CanonReferenceImage",
    "CanonResponse",
    "CommentRequest",
    "CommentResponse",
    "ConflictChoiceRequest",
    "ConflictChoiceResponse",
    "CreateSessionRequest",
    "ErrorBody",
    "ErrorResponse",
    "IntentRequest",
    "IntentResponse",
    "LoginRequest",
    "PageResponse",
    "RegisterRequest",
    "SeekRequest",
    "SeekResponse",
    "SessionResponse",
    "ShotResponse",
    "TokenResponse",
    "UserResponse",
]
