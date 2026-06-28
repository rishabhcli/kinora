"""Pydantic models and the cost-accounting ``Usage`` unit for the provider layer.

``Usage`` is the single currency the budget service (a later phase) subscribes
to: every provider call emits one. The scarce, hard-capped resource is
``video_seconds`` (§11.1); tokens/images/audio are tracked for completeness and
per-shot telemetry (§12.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Cost accounting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Usage:
    """One unit of provider spend, recorded by every call.

    Attributes:
        model: The model id the spend is attributed to.
        operation: Coarse op label (``chat``/``vl``/``image``/``tts``/``video``).
        input_tokens: Prompt tokens billed.
        output_tokens: Completion tokens billed.
        images: Number of images generated/edited.
        audio_seconds: Seconds of synthesized audio.
        video_seconds: Seconds of generated video — the budget-critical resource.
        latency_ms: Wall-clock latency of the call, when measured.
        request_id: Provider request id for cross-referencing.
    """

    model: str
    operation: str
    input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    audio_seconds: float = 0.0
    video_seconds: float = 0.0
    latency_ms: float | None = None
    request_id: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def with_latency(self, latency_ms: float) -> Usage:
        """Return a copy stamped with a measured latency."""
        return replace(self, latency_ms=latency_ms)

    def as_log_fields(self) -> dict[str, Any]:
        """Structured-log-safe fields (counts only — never prompt content)."""
        fields: dict[str, Any] = {
            "model": self.model,
            "operation": self.operation,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
        if self.images:
            fields["images"] = self.images
        if self.audio_seconds:
            fields["audio_seconds"] = round(self.audio_seconds, 3)
        if self.video_seconds:
            fields["video_seconds"] = round(self.video_seconds, 3)
        if self.latency_ms is not None:
            fields["latency_ms"] = round(self.latency_ms, 1)
        if self.request_id:
            fields["request_id"] = self.request_id
        return fields


@dataclass
class UsageTotals:
    """Mutable in-memory accumulator of :class:`Usage` events.

    Doubles as the default cost sink so the spend ledger is inspectable in tests
    and local runs before the budget service is wired in.
    """

    events: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    audio_seconds: float = 0.0
    video_seconds: float = 0.0
    by_operation: dict[str, int] = field(default_factory=dict)

    def add(self, usage: Usage) -> None:
        self.events += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.images += usage.images
        self.audio_seconds += usage.audio_seconds
        self.video_seconds += usage.video_seconds
        self.by_operation[usage.operation] = self.by_operation.get(usage.operation, 0) + 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# --------------------------------------------------------------------------- #
# Chat / VL
# --------------------------------------------------------------------------- #


class ToolCall(BaseModel):
    """A function/tool call requested by the model (OpenAI-compatible shape)."""

    id: str | None = None
    type: str = "function"
    name: str
    arguments: str = "{}"


class ChatResult(BaseModel):
    """Result of a chat / VL completion."""

    model_config = ConfigDict(extra="ignore")

    text: str
    model: str
    finish_reason: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# --------------------------------------------------------------------------- #
# TTS
# --------------------------------------------------------------------------- #


class TtsWord(BaseModel):
    """One word's timing in synthesized narration (seconds from clip start)."""

    text: str
    t_start: float
    t_end: float


class TtsResult(BaseModel):
    """Synthesized narration plus the word-timing map that drives karaoke +
    page-turn (§9.4)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    audio_bytes: bytes
    sample_rate: int
    duration_s: float
    word_timestamps: list[TtsWord] = Field(default_factory=list)
    #: How ``word_timestamps`` were derived. ``asr`` = real forced alignment via
    #: DashScope ASR over the waveform; ``model`` = timestamps emitted inline by
    #: the TTS model; ``proportional`` = distributed across the *measured* audio
    #: duration weighted by token length when no word-level aligner is available.
    alignment: Literal["asr", "model", "proportional"] = "proportional"
    voice_id: str | None = None
    model: str
    audio_format: str = "wav"


# --------------------------------------------------------------------------- #
# Video (Wan)
# --------------------------------------------------------------------------- #


class WanMode(StrEnum):
    """Hosted Wan render modes from the decision tree (§9.3)."""

    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"
    FIRST_LAST_FRAME = "first_last_frame"
    VIDEO_CONTINUATION = "video_continuation"
    INSTRUCTION_EDIT = "instruction_edit"


class WanSpec(BaseModel):
    """A fully-resolved request for one Wan render.

    Image/video inputs are passed as URLs (the render worker persists locked
    references and endpoint frames to object storage and hands their signed URLs
    here). ``model`` overrides the per-mode default resolved from settings.
    """

    model_config = ConfigDict(use_enum_values=False)

    mode: WanMode
    prompt: str = ""
    negative_prompt: str | None = None
    #: r2v: locked character/appearance reference image URLs.
    reference_image_urls: list[str] = Field(default_factory=list)
    #: r2v: optional cloned-voice reference audio URL.
    reference_voice_url: str | None = None
    #: i2v / continuation: the single driving / start frame URL.
    image_url: str | None = None
    #: first-last-frame: start + end composition URLs.
    first_frame_url: str | None = None
    last_frame_url: str | None = None
    #: continuation / instruction_edit: the prior accepted clip URL.
    source_video_url: str | None = None
    seed: int | None = None
    duration_s: int = 5
    resolution: str = "720P"
    watermark: bool = False
    prompt_extend: bool = False
    model: str | None = None
    #: Carried through for idempotency/telemetry; not sent to the API.
    shot_id: str | None = None


class VideoResult(BaseModel):
    """Result of a completed Wan render."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    duration_s: float
    model: str
    mode: WanMode
    provider_task_id: str | None = None
    clip_url: str | None = None
    clip_bytes: bytes | None = None
    last_frame_bytes: bytes | None = None


__all__ = [
    "ChatResult",
    "ToolCall",
    "TtsResult",
    "TtsWord",
    "Usage",
    "UsageTotals",
    "VideoResult",
    "WanMode",
    "WanSpec",
]
