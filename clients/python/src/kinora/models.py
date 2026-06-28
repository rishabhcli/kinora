"""Typed wire models for the Kinora API (Python SDK).

These mirror ``backend/app/api/schemas.py`` and ``backend/app/films/contract.py``.
Each response model is a frozen dataclass with a ``from_dict`` classmethod that is
*tolerant of unknown fields* (forward-compatible): anything the model does not
name is preserved in ``extra``. Request bodies are built with plain helpers /
TypedDicts so the caller passes exactly what the endpoint expects.

Kept honest against the backend by ``clients/contract-drift/check_drift.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, TypeVar

T = TypeVar("T", bound="_Model")

Json = dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Model:
    """Base for response models: a forward-compatible ``from_dict``.

    Unknown keys are dropped into ``extra`` rather than rejected, so the SDK does
    not break when the backend adds a field.
    """

    extra: Json = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls: type[T], data: Json) -> T:
        known = {f.name for f in fields(cls)} - {"extra"}
        kwargs: Json = {}
        extra: Json = {}
        for key, value in data.items():
            if key in known:
                kwargs[key] = value
            else:
                extra[key] = value
        return cls(**kwargs, extra=extra)

    def get(self, key: str, default: Any = None) -> Any:
        """Read a named or extra field by string key (convenience)."""
        if key in {f.name for f in fields(self)}:
            return getattr(self, key)
        return self.extra.get(key, default)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TokenResponse(_Model):
    access_token: str = ""
    token_type: str = "bearer"
    expires_in: int = 0


@dataclass(frozen=True, slots=True)
class UserResponse(_Model):
    id: str = ""
    email: str = ""
    created_at: str | None = None


# --------------------------------------------------------------------------- #
# Books
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BookResponse(_Model):
    id: str = ""
    title: str = ""
    author: str | None = None
    status: str = ""
    num_pages: int | None = None
    art_direction: str | None = None
    created_at: str | None = None
    progress: float | None = None
    stage: str | None = None
    cover_url: str | None = None


@dataclass(frozen=True, slots=True)
class WordBox(_Model):
    word_index: int = 0
    text: str = ""
    bbox: list[float] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PageResponse(_Model):
    book_id: str = ""
    page_number: int = 0
    image_url: str | None = None
    text: str | None = None
    word_boxes: list[Json] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ShotResponse(_Model):
    shot_id: str = ""
    beat_id: str | None = None
    scene_id: str | None = None
    source_span: Json | None = None
    status: str = ""
    render_mode: str | None = None
    duration_s: float | None = None
    qa: Json | None = None
    clip_url: str | None = None
    reference_image_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CanonReferenceImage(_Model):
    oss_url: str = ""
    oss_key: str | None = None
    pose: str | None = None
    locked: bool | None = None


@dataclass(frozen=True, slots=True)
class CanonAppearance(_Model):
    description: str | None = None
    reference_images: list[Json] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CanonEntityResponse(_Model):
    id: str = ""
    type: str = ""
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    description: str | None = None
    appearance: Json | None = None
    style_tokens: Json | None = None
    voice: Json | None = None
    version: int = 1
    valid_from_beat: int | None = None
    valid_to_beat: int | None = None
    first_appearance: Json | None = None


@dataclass(frozen=True, slots=True)
class CanonStateResponse(_Model):
    id: str = ""
    subject_entity_key: str = ""
    predicate: str = ""
    object_value: str = ""
    valid_from_beat: int = 0
    valid_to_beat: int | None = None
    version: int = 1
    active: bool = True
    source_span: Json | None = None


@dataclass(frozen=True, slots=True)
class CanonResponse(_Model):
    book_id: str = ""
    entities: list[CanonEntityResponse] = field(default_factory=list)
    states: list[CanonStateResponse] = field(default_factory=list)
    markdown: str | None = None

    @classmethod
    def from_dict(cls, data: Json) -> CanonResponse:
        base = super().from_dict(data)
        return CanonResponse(
            book_id=base.book_id,
            entities=[CanonEntityResponse.from_dict(e) for e in data.get("entities", [])],
            states=[CanonStateResponse.from_dict(s) for s in data.get("states", [])],
            markdown=data.get("markdown"),
            extra=base.extra,
        )


# --------------------------------------------------------------------------- #
# Films / sync map
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SceneFilm(_Model):
    scene_id: str = ""
    event_id: str = ""
    book_id: str = ""
    scene_index: int = 0
    event_index: int = 0
    page_start: int = 0
    page_end: int = 0
    word_range: list[int] = field(default_factory=list)
    stitched: bool = False
    oss_url: str | None = None
    url_expires_at: str | None = None
    duration_s: float | None = None
    shot_count: int = 0
    sync_map: Json = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EventFilm(_Model):
    event_id: str = ""
    event_index: int = 0
    book_id: str = ""
    page_start: int = 0
    page_end: int = 0
    word_range: list[int] = field(default_factory=list)
    stitched: bool = False
    oss_url: str | None = None
    url_expires_at: str | None = None
    duration_s: float | None = None
    shot_count: int = 0
    sync_map: Json = field(default_factory=dict)
    scenes: list[Json] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RestoreState(_Model):
    session_id: str = ""
    focus_word: int = 0
    current_event_index: int | None = None
    current_scene_id: str | None = None
    mode: str = "viewer"


@dataclass(frozen=True, slots=True)
class EventsResponse(_Model):
    book_id: str = ""
    url_ttl_s: int = 0
    events: list[EventFilm] = field(default_factory=list)
    restore: RestoreState | None = None

    @classmethod
    def from_dict(cls, data: Json) -> EventsResponse:
        base = super().from_dict(data)
        restore = data.get("restore")
        return EventsResponse(
            book_id=base.book_id,
            url_ttl_s=base.url_ttl_s,
            events=[EventFilm.from_dict(e) for e in data.get("events", [])],
            restore=RestoreState.from_dict(restore) if isinstance(restore, dict) else None,
            extra=base.extra,
        )


# --------------------------------------------------------------------------- #
# Sessions / intent / seek
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SessionResponse(_Model):
    session_id: str = ""
    book_id: str = ""
    focus_word: int = 0
    velocity_wps: float = 0.0
    mode: str = "viewer"
    committed_seconds_ahead: float = 0.0
    bursting: bool = False
    budget_remaining_s: float | None = None
    inflight: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IntentResponse(_Model):
    session_id: str = ""
    settled: bool = False
    allow_promotion: bool = False
    idle: bool = False
    bursting: bool = False
    committed_seconds_ahead: float = 0.0
    promoted: list[str] = field(default_factory=list)
    keyframed: list[str] = field(default_factory=list)
    cancelled: int = 0


@dataclass(frozen=True, slots=True)
class SeekResponse(_Model):
    session_id: str = ""
    word: int = 0
    cancelled: int = 0
    bridge_beat: str | None = None
    committed_seconds_ahead: float = 0.0


# --------------------------------------------------------------------------- #
# Director tools
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DirectingPriorView(_Model):
    kind: str = ""
    bias: float = 0.0
    weight: float = 0.0
    label: str = ""
    detail: str = ""
    applied: bool = False
    applied_value: str | None = None
    last_note: str | None = None


@dataclass(frozen=True, slots=True)
class CommentResponse(_Model):
    shot_id: str = ""
    agent: str = ""
    aspect: str = ""
    message: str = ""
    job_id: str | None = None
    learned: list[DirectingPriorView] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Json) -> CommentResponse:
        base = super().from_dict(data)
        return CommentResponse(
            shot_id=base.shot_id,
            agent=base.agent,
            aspect=base.aspect,
            message=base.message,
            job_id=data.get("job_id"),
            learned=[DirectingPriorView.from_dict(p) for p in data.get("learned", [])],
            extra=base.extra,
        )


@dataclass(frozen=True, slots=True)
class CanonEditResponse(_Model):
    entity_key: str = ""
    version: int = 1
    affected_shot_ids: list[str] = field(default_factory=list)
    skipped_shots: int = 0


@dataclass(frozen=True, slots=True)
class ConflictChoiceResponse(_Model):
    conflict_id: str = ""
    option: str = ""
    status: str = "recorded"
    shot_id: str | None = None
    reasoning: str | None = None


@dataclass(frozen=True, slots=True)
class ConflictRecordResponse(_Model):
    conflict_id: str = ""
    shot_id: str | None = None
    claim: str | None = None
    canon_fact: str | None = None
    raised_by: str | None = None
    current_beat: str | None = None
    options: list[Json] = field(default_factory=list)
    resolved: bool = False
    chosen_option: str | None = None
    reasoning: str | None = None


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DirectingStyleResponse(_Model):
    scope: str = ""
    book_id: str | None = None
    priors: list[DirectingPriorView] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Json) -> DirectingStyleResponse:
        base = super().from_dict(data)
        return DirectingStyleResponse(
            scope=base.scope,
            book_id=data.get("book_id"),
            priors=[DirectingPriorView.from_dict(p) for p in data.get("priors", [])],
            extra=base.extra,
        )


@dataclass(frozen=True, slots=True)
class ResetPrefsResponse(_Model):
    scope: str = ""
    book_id: str | None = None
    cleared: int = 0


# --------------------------------------------------------------------------- #
# Eval
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BufferTracePoint(_Model):
    t: float = 0.0
    committed_seconds_ahead: float = 0.0
    low: float = 0.0
    high: float = 0.0


__all__ = [
    "BookResponse",
    "BufferTracePoint",
    "CanonAppearance",
    "CanonEditResponse",
    "CanonEntityResponse",
    "CanonReferenceImage",
    "CanonResponse",
    "CanonStateResponse",
    "CommentResponse",
    "ConflictChoiceResponse",
    "ConflictRecordResponse",
    "DirectingPriorView",
    "DirectingStyleResponse",
    "EventFilm",
    "EventsResponse",
    "IntentResponse",
    "Json",
    "PageResponse",
    "ResetPrefsResponse",
    "RestoreState",
    "SceneFilm",
    "SeekResponse",
    "SessionResponse",
    "ShotResponse",
    "TokenResponse",
    "UserResponse",
    "WordBox",
]
