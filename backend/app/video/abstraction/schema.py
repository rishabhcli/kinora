"""The canonical, provider-neutral request / result schema.

Kinora's render seam speaks :class:`app.providers.types.WanSpec` /
:class:`app.providers.types.VideoResult` — shapes named after a specific model
family. The Universal Video Provider abstraction introduces a *neutral* pair that
any model can express, and the :class:`~app.video.abstraction.normalizer.Normalizer`
maps Wan ↔ canonical losslessly (§9.2/§9.3). Adapters consume a
:class:`CanonicalVideoRequest` and emit a :class:`CanonicalVideoResult`; the
Generator keeps building ``WanSpec`` and the normalizer bridges the two.

Design notes:

* **References are URL-or-bytes.** Hosted providers want signed URLs; a
  self-hosted lane may want inline bytes. A :class:`MediaRef` carries either,
  plus the typed role (``first_frame`` / ``last_frame`` / ``reference`` /
  ``source_video`` / ``reference_voice``) the §9.3 modes need, so one request
  shape serves every mode without per-mode subclasses.
* **The spend-critical field is ``duration_s``.** It is what the budget reserves
  (§11.1), so it is first-class and validated.
* **A request carries its own idempotency key.** ``shot_id`` ties a render back to
  the per-shot state machine (§9.7) for dedupe/telemetry and is never sent to a
  model.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .capability import VideoMode

# --------------------------------------------------------------------------- #
# Media references
# --------------------------------------------------------------------------- #


class MediaRole(StrEnum):
    """The role a conditioning input plays in a render (drives §9.3 mode wiring)."""

    #: i2v / continuation driving frame; FLF start frame.
    FIRST_FRAME = "first_frame"
    #: FLF end-composition frame.
    LAST_FRAME = "last_frame"
    #: r2v locked appearance/identity reference (may repeat).
    REFERENCE = "reference"
    #: continuation / instruction-edit prior accepted clip.
    SOURCE_VIDEO = "source_video"
    #: r2v cloned-voice reference audio.
    REFERENCE_VOICE = "reference_voice"


class MediaRef(BaseModel):
    """A single conditioning input: a URL *or* inline bytes, plus its role.

    Exactly one of ``url`` / ``data`` must be set. ``data`` is base64-or-raw bytes
    for the inline (``data:``-URI / self-hosted) path; ``url`` is the hosted
    signed-URL path. ``mime`` is advisory for the bytes path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    role: MediaRole
    url: str | None = None
    data: bytes | None = None
    mime: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> MediaRef:
        if (self.url is None) == (self.data is None):
            raise ValueError("MediaRef requires exactly one of `url` or `data`")
        if self.url is not None and not self.url.strip():
            raise ValueError("MediaRef.url must be non-empty")
        return self

    @property
    def is_inline(self) -> bool:
        """True when the reference carries inline bytes rather than a URL."""
        return self.data is not None


# --------------------------------------------------------------------------- #
# Canonical request
# --------------------------------------------------------------------------- #


class CanonicalVideoRequest(BaseModel):
    """A provider-neutral request for one video render.

    Maps losslessly to/from :class:`app.providers.types.WanSpec` via the
    normalizer. Unset optional fields mean "let the provider/capability default
    decide" — the normalizer fills resolution/fps from the chosen capability.

    Attributes:
        mode: the canonical §9.3 render mode.
        prompt: the text prompt (camera grammar already folded in upstream).
        negative_prompt: optional negative prompt (only sent if supported).
        media: ordered conditioning inputs (frames / references / source clip).
        seed: optional deterministic seed.
        duration_s: requested clip length in seconds (budget-critical, §11.1).
        resolution / aspect_ratio / fps: optional target geometry.
        watermark / prompt_extend: provider toggles (ignored where unsupported).
        model: optional explicit model-id override within the chosen provider.
        shot_id: idempotency / telemetry key (never sent to the model).
        provider_options: opaque extra knobs passed straight through to the
            adapter's native request for provider-specific features the canonical
            schema does not (yet) model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: VideoMode
    prompt: str = ""
    negative_prompt: str | None = None
    media: tuple[MediaRef, ...] = ()
    seed: int | None = None
    duration_s: float = Field(default=5.0, gt=0)
    resolution: str | None = None
    aspect_ratio: str | None = None
    fps: int | None = Field(default=None, gt=0)
    watermark: bool = False
    prompt_extend: bool = False
    model: str | None = None
    shot_id: str | None = None
    provider_options: dict[str, object] = Field(default_factory=dict)

    # -- role accessors (kept tiny so adapters don't re-filter media) ----- #

    def media_for(self, role: MediaRole) -> tuple[MediaRef, ...]:
        """All media refs with the given ``role``, in request order."""
        return tuple(m for m in self.media if m.role is role)

    def first_media(self, role: MediaRole) -> MediaRef | None:
        """The first media ref with ``role``, or ``None``."""
        for m in self.media:
            if m.role is role:
                return m
        return None

    @property
    def references(self) -> tuple[MediaRef, ...]:
        """The ordered r2v identity references."""
        return self.media_for(MediaRole.REFERENCE)

    def idempotency_key(self) -> str:
        """A stable digest of the request's rendered content.

        Used by the per-shot state machine (§9.7) and the registry's dedupe to
        recognise an identical render. Excludes nothing meaningful: mode, prompts,
        seed, duration, geometry, model, and the *roles + sources* of every media
        ref (URL or bytes-hash) all participate, so two requests collide iff they
        would produce the same clip. ``shot_id`` is folded in last so the same
        creative request for two different shots stays distinct.
        """
        h = hashlib.sha256()
        h.update(self.mode.value.encode())
        h.update(b"\x00")
        h.update(self.prompt.encode())
        h.update(b"\x00")
        h.update((self.negative_prompt or "").encode())
        h.update(b"\x00")
        h.update(repr(self.seed).encode())
        h.update(f"|{self.duration_s}|{self.resolution}|{self.aspect_ratio}|{self.fps}".encode())
        h.update(f"|{self.watermark}|{self.prompt_extend}|{self.model}".encode())
        for m in self.media:
            h.update(b"\x01")
            h.update(m.role.value.encode())
            if m.url is not None:
                h.update(b"u")
                h.update(m.url.encode())
            else:
                h.update(b"b")
                h.update(hashlib.sha256(m.data or b"").digest())
        h.update(b"\x02")
        h.update((self.shot_id or "").encode())
        return h.hexdigest()


# --------------------------------------------------------------------------- #
# Canonical result + task handle
# --------------------------------------------------------------------------- #


class TaskState(StrEnum):
    """Lifecycle of an async render task (maps to the §9.7 Rendering states)."""

    PENDING = "pending"  # submitted, not yet running
    RUNNING = "running"  # in progress
    SUCCEEDED = "succeeded"  # terminal OK — a clip is available
    FAILED = "failed"  # terminal error
    CANCELED = "canceled"  # terminal — cancelled before completion

    @property
    def is_terminal(self) -> bool:
        return self in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED)


class VideoTaskHandle(BaseModel):
    """An opaque handle to a submitted render, returned by ``submit``.

    Carries the provider id + the provider's own task id so a later ``poll`` /
    ``fetch`` / ``cancel`` routes back to the same provider. Synchronous providers
    return a handle already in a terminal state with the result attached.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    state: TaskState = TaskState.PENDING
    #: Echoed back so callers can correlate without holding the request.
    shot_id: str | None = None
    #: A synchronous provider may attach the finished result here at submit time.
    inline_result: CanonicalVideoResult | None = None


class CanonicalVideoResult(BaseModel):
    """A provider-neutral completed render.

    Maps losslessly to/from :class:`app.providers.types.VideoResult` via the
    normalizer. The clip is carried as a URL and/or bytes (whichever the provider
    produced); ``last_frame`` is the continuation anchor written to the canon
    (§9.6). ``audio_*`` are populated only when the provider emits audio.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    provider_id: str = Field(min_length=1)
    mode: VideoMode
    model: str = ""
    duration_s: float = Field(ge=0)
    clip_url: str | None = None
    clip_bytes: bytes | None = None
    last_frame_bytes: bytes | None = None
    #: Resolved geometry the provider actually produced (when reported).
    resolution: str | None = None
    fps: int | None = None
    #: Provider task id for cross-referencing (§9.7 / telemetry).
    provider_task_id: str | None = None
    seed: int | None = None
    #: Audio track when the provider emits one (else absent).
    audio_bytes: bytes | None = None
    sample_rate: int | None = None

    @model_validator(mode="after")
    def _has_clip(self) -> CanonicalVideoResult:
        if self.clip_url is None and self.clip_bytes is None:
            raise ValueError("CanonicalVideoResult must carry a clip_url or clip_bytes")
        return self


__all__ = [
    "CanonicalVideoRequest",
    "CanonicalVideoResult",
    "MediaRef",
    "MediaRole",
    "TaskState",
    "VideoTaskHandle",
]
