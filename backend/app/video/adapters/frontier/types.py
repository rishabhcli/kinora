"""Canonical request/result models, capability profiles, and the adapter Protocol.

This module is deliberately self-contained: it defines a *local* Protocol
(:class:`UniversalVideoProvider`) that mirrors
:meth:`app.providers.video_router.VideoBackend.render` **plus** the richer
``capabilities()`` / ``submit()`` / ``poll()`` / ``fetch()`` surface, so a frontier
adapter integrates whether or not a wider video-abstraction package has merged yet:

* It satisfies the existing :class:`~app.providers.video_router.VideoBackend`
  (``name`` / ``render`` / ``healthy``) → drops straight into a
  :class:`~app.providers.video_router.VideoRouter`.
* It *also* exposes the multi-phase async-job lifecycle (submit → poll → fetch) so a
  scheduler that wants to overlap submission and download, or persist the job handle
  across a restart, can drive the adapter at a finer grain.

The canonical request (:class:`FrontierRequest`) is Kinora-agnostic; the helper
:func:`from_wan_spec` maps the in-repo :class:`~app.providers.types.WanSpec` onto it,
so today's callers (the Generator / render pipeline) keep speaking ``WanSpec``.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.providers.types import VideoResult, WanMode, WanSpec

from .errors import FrontierUnsupportedCapability

# --------------------------------------------------------------------------- #
# Canonical render mode (provider-agnostic; a superset mapped from WanMode)
# --------------------------------------------------------------------------- #


class VideoMode(StrEnum):
    """The canonical generation modes a frontier provider may support.

    Mapped 1:1 from :class:`~app.providers.types.WanMode` by :func:`mode_from_wan`,
    but kept as its own enum so the frontier layer is not bound to the Wan vocabulary.
    """

    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"
    FIRST_LAST_FRAME = "first_last_frame"
    VIDEO_CONTINUATION = "video_continuation"
    INSTRUCTION_EDIT = "instruction_edit"


_WAN_TO_MODE: dict[WanMode, VideoMode] = {
    WanMode.TEXT_TO_VIDEO: VideoMode.TEXT_TO_VIDEO,
    WanMode.IMAGE_TO_VIDEO: VideoMode.IMAGE_TO_VIDEO,
    WanMode.REFERENCE_TO_VIDEO: VideoMode.REFERENCE_TO_VIDEO,
    WanMode.FIRST_LAST_FRAME: VideoMode.FIRST_LAST_FRAME,
    WanMode.VIDEO_CONTINUATION: VideoMode.VIDEO_CONTINUATION,
    WanMode.INSTRUCTION_EDIT: VideoMode.INSTRUCTION_EDIT,
}
_MODE_TO_WAN: dict[VideoMode, WanMode] = {v: k for k, v in _WAN_TO_MODE.items()}


def mode_from_wan(mode: WanMode) -> VideoMode:
    """Map a :class:`~app.providers.types.WanMode` to the canonical :class:`VideoMode`."""
    return _WAN_TO_MODE[mode]


def mode_to_wan(mode: VideoMode) -> WanMode:
    """Map a canonical :class:`VideoMode` back to a :class:`~app.providers.types.WanMode`."""
    return _MODE_TO_WAN[mode]


# --------------------------------------------------------------------------- #
# Capability profile — the *real* envelope each provider declares
# --------------------------------------------------------------------------- #


class CapabilityProfile(BaseModel):
    """The real capability envelope of one frontier model.

    Adapters declare this from the provider's published docs; the base adapter
    validates a :class:`FrontierRequest` against it *before* spending a network call,
    raising :class:`~app.video.adapters.frontier.errors.FrontierUnsupportedCapability`
    on any violation. ``durations_s`` / ``resolutions`` / ``aspect_ratios`` /
    ``fps_options`` are the *exact discrete sets* the provider accepts (these models
    do not take arbitrary values), while ``max_reference_images`` /
    ``max_prompt_chars`` are upper bounds.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    #: Generation modes this model supports.
    modes: frozenset[VideoMode]
    #: Exact clip durations (seconds) the provider accepts.
    durations_s: tuple[float, ...]
    #: Accepted resolution labels (e.g. "720p", "1080p", "4k").
    resolutions: tuple[str, ...]
    #: Accepted aspect ratios (e.g. "16:9", "9:16", "1:1").
    aspect_ratios: tuple[str, ...]
    #: Accepted frame rates; empty → provider does not expose an fps knob.
    fps_options: tuple[int, ...] = ()
    #: Whether the provider accepts an explicit seed for reproducibility.
    supports_seed: bool = False
    #: Whether the provider accepts a negative prompt.
    supports_negative_prompt: bool = False
    #: Max reference/start/end images the provider accepts (0 → text-only).
    max_reference_images: int = 0
    #: Max prompt length in characters (0 → no documented limit).
    max_prompt_chars: int = 0

    def supports_mode(self, mode: VideoMode) -> bool:
        return mode in self.modes

    def nearest_duration(self, duration_s: float) -> float:
        """Snap a requested duration to the nearest *supported* discrete value.

        Frontier models take a fixed menu of durations; the scheduler asks for an
        arbitrary target, so the adapter snaps to the closest legal value rather
        than rejecting (ties resolve to the shorter clip — cheaper video-seconds).
        """
        if not self.durations_s:
            return duration_s
        return min(self.durations_s, key=lambda d: (abs(d - duration_s), d))

    def default_resolution(self) -> str:
        return self.resolutions[0] if self.resolutions else ""

    def default_aspect_ratio(self) -> str:
        return self.aspect_ratios[0] if self.aspect_ratios else ""


# --------------------------------------------------------------------------- #
# Canonical request
# --------------------------------------------------------------------------- #


class FrontierRequest(BaseModel):
    """A provider-agnostic, fully-resolved request for one frontier render.

    Image/video inputs are URLs or ``data:`` URIs (the same convention the Generator
    already uses). The adapter maps this onto the provider's native body.
    """

    model_config = ConfigDict(use_enum_values=False)

    mode: VideoMode
    prompt: str = ""
    negative_prompt: str | None = None
    #: r2v: locked character/appearance reference image URLs/URIs.
    reference_image_urls: list[str] = Field(default_factory=list)
    #: i2v / continuation: the single driving / start frame.
    image_url: str | None = None
    #: first-last-frame: start + end composition.
    first_frame_url: str | None = None
    last_frame_url: str | None = None
    #: continuation / instruction_edit: the prior accepted clip.
    source_video_url: str | None = None
    seed: int | None = None
    duration_s: float = 5.0
    resolution: str = "720p"
    aspect_ratio: str = "16:9"
    fps: int | None = None
    #: Carried through for idempotency/telemetry; not sent to the provider.
    shot_id: str | None = None
    #: Optional explicit model id override (else the adapter's default).
    model: str | None = None

    def primary_image(self) -> str | None:
        """The single conditioning image for image-to-video-ish modes, by priority."""
        return (
            self.image_url
            or self.first_frame_url
            or (self.reference_image_urls[0] if self.reference_image_urls else None)
        )


def from_wan_spec(spec: WanSpec) -> FrontierRequest:
    """Map an in-repo :class:`~app.providers.types.WanSpec` onto a canonical request."""
    return FrontierRequest(
        mode=mode_from_wan(spec.mode),
        prompt=spec.prompt,
        negative_prompt=spec.negative_prompt,
        reference_image_urls=list(spec.reference_image_urls),
        image_url=spec.image_url,
        first_frame_url=spec.first_frame_url,
        last_frame_url=spec.last_frame_url,
        source_video_url=spec.source_video_url,
        seed=spec.seed,
        duration_s=float(spec.duration_s),
        resolution=_resolution_label(spec.resolution),
        shot_id=spec.shot_id,
        model=spec.model,
    )


def _resolution_label(resolution: str) -> str:
    """Normalise a WanSpec resolution token ("720P") to a lowercase label ("720p")."""
    return (resolution or "720p").strip().lower()


# --------------------------------------------------------------------------- #
# Async-job lifecycle handles
# --------------------------------------------------------------------------- #


class JobStatus(StrEnum):
    """Canonical lifecycle status of a frontier render job."""

    PENDING = "pending"  # accepted, queued/running, not yet terminal
    SUCCEEDED = "succeeded"  # a result asset URL is available
    FAILED = "failed"  # terminal failure
    CANCELED = "canceled"  # terminal cancel


class SubmitHandle(BaseModel):
    """An opaque-ish handle to a submitted job, returned by ``submit()``.

    Carries enough to poll and (eventually) fetch the asset across processes/restarts:
    the provider's job id plus the model it ran on.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    job_id: str


class PollResult(BaseModel):
    """The outcome of one ``poll()`` of a :class:`SubmitHandle`."""

    model_config = ConfigDict(frozen=True)

    status: JobStatus
    #: Result asset URL when ``status is SUCCEEDED`` (expires — fetch eagerly).
    asset_url: str | None = None
    #: Optional last-frame/thumbnail asset URL when the provider exposes one.
    last_frame_url: str | None = None
    #: Provider-reported clip duration (seconds) when known.
    duration_s: float | None = None
    #: Provider-reported failure message when ``status`` is FAILED/CANCELED.
    detail: str | None = None
    #: Progress 0..1 when the provider reports it (else None).
    progress: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status is not JobStatus.PENDING


class FetchedClip(BaseModel):
    """The downloaded clip bytes (and optional last-frame bytes) from ``fetch()``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    clip_bytes: bytes
    last_frame_bytes: bytes | None = None
    clip_url: str | None = None
    duration_s: float = 0.0


# --------------------------------------------------------------------------- #
# The adapter Protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class UniversalVideoProvider(Protocol):
    """The interface every frontier adapter implements.

    A superset of :class:`~app.providers.video_router.VideoBackend`:

    * ``name`` / ``render`` / ``healthy`` → :class:`VideoBackend`-compatible, so an
      adapter drops into a :class:`~app.providers.video_router.VideoRouter` unchanged.
    * ``capabilities`` → the declared :class:`CapabilityProfile`.
    * ``submit`` / ``poll`` / ``fetch`` → the explicit async-job lifecycle, for
      callers that want to overlap or persist the job rather than block in ``render``.

    ``render`` is the convenience composition of ``submit`` → poll-loop → ``fetch``.
    """

    name: str

    def capabilities(self) -> CapabilityProfile:
        """The provider's declared capability envelope."""
        ...

    async def submit(self, request: FrontierRequest) -> SubmitHandle:
        """Validate + submit the request; return a handle to poll. Gated."""
        ...

    async def poll(self, handle: SubmitHandle) -> PollResult:
        """Poll a submitted job once for its current status."""
        ...

    async def fetch(self, poll_result: PollResult) -> FetchedClip:
        """Download the produced asset bytes (re-download eagerly; URLs expire)."""
        ...

    async def render(self, spec: WanSpec) -> VideoResult:
        """Submit, poll to completion, fetch, and return a :class:`VideoResult`."""
        ...

    async def healthy(self) -> bool:
        """Cheap liveness probe (no render spend)."""
        ...


def validate_against_profile(request: FrontierRequest, profile: CapabilityProfile) -> None:
    """Raise :class:`FrontierUnsupportedCapability` for any capability violation.

    The single local pre-flight every adapter runs before a network call. It checks
    the mode, resolution, aspect ratio, fps, prompt length, reference count, and
    seed/negative-prompt support. Duration is *snapped* (not rejected) by the base
    adapter via :meth:`CapabilityProfile.nearest_duration`, so it is not checked here.
    """
    p = profile
    if not p.supports_mode(request.mode):
        raise FrontierUnsupportedCapability(
            f"{p.provider}:{p.model} does not support mode {request.mode.value}",
            provider=p.provider,
        )
    if p.resolutions and request.resolution not in p.resolutions:
        raise FrontierUnsupportedCapability(
            f"{p.provider}:{p.model} resolution {request.resolution!r} not in "
            f"{list(p.resolutions)}",
            provider=p.provider,
        )
    if p.aspect_ratios and request.aspect_ratio not in p.aspect_ratios:
        raise FrontierUnsupportedCapability(
            f"{p.provider}:{p.model} aspect ratio {request.aspect_ratio!r} not in "
            f"{list(p.aspect_ratios)}",
            provider=p.provider,
        )
    if request.fps is not None and p.fps_options and request.fps not in p.fps_options:
        raise FrontierUnsupportedCapability(
            f"{p.provider}:{p.model} fps {request.fps} not in {list(p.fps_options)}",
            provider=p.provider,
        )
    if request.seed is not None and not p.supports_seed:
        raise FrontierUnsupportedCapability(
            f"{p.provider}:{p.model} does not support an explicit seed",
            provider=p.provider,
        )
    if request.negative_prompt and not p.supports_negative_prompt:
        raise FrontierUnsupportedCapability(
            f"{p.provider}:{p.model} does not support a negative prompt",
            provider=p.provider,
        )
    if p.max_prompt_chars and len(request.prompt) > p.max_prompt_chars:
        raise FrontierUnsupportedCapability(
            f"{p.provider}:{p.model} prompt exceeds {p.max_prompt_chars} chars "
            f"({len(request.prompt)})",
            provider=p.provider,
        )
    _validate_reference_count(request, p)


def _validate_reference_count(request: FrontierRequest, p: CapabilityProfile) -> None:
    used = _images_used(request)
    if p.max_reference_images == 0 and used:
        raise FrontierUnsupportedCapability(
            f"{p.provider}:{p.model} is text-only but {used} image input(s) were given",
            provider=p.provider,
        )
    if p.max_reference_images and used > p.max_reference_images:
        raise FrontierUnsupportedCapability(
            f"{p.provider}:{p.model} accepts at most {p.max_reference_images} image "
            f"input(s); {used} were given",
            provider=p.provider,
        )


def _images_used(request: FrontierRequest) -> int:
    """Count the image inputs that are *relevant* to the request's mode."""
    if request.mode is VideoMode.REFERENCE_TO_VIDEO:
        return len(request.reference_image_urls)
    if request.mode is VideoMode.FIRST_LAST_FRAME:
        return sum(1 for u in (request.first_frame_url, request.last_frame_url) if u)
    if request.mode in (VideoMode.IMAGE_TO_VIDEO, VideoMode.VIDEO_CONTINUATION):
        return 1 if request.primary_image() else 0
    return 0


def supported_modes_summary(profiles: Sequence[CapabilityProfile]) -> dict[str, list[str]]:
    """A small {provider:model → [modes]} summary for telemetry/registry inspection."""
    return {f"{p.provider}:{p.model}": sorted(m.value for m in p.modes) for p in profiles}


__all__ = [
    "CapabilityProfile",
    "FetchedClip",
    "FrontierRequest",
    "JobStatus",
    "PollResult",
    "SubmitHandle",
    "UniversalVideoProvider",
    "VideoMode",
    "from_wan_spec",
    "mode_from_wan",
    "mode_to_wan",
    "supported_modes_summary",
    "validate_against_profile",
]
