"""The :class:`VideoCapability` contract — what *one* video-gen model can do.

Kinora's §9.3 Wan-mode decision tree assumes a specific model family (hosted Wan
2.x / 2.7 via DashScope). The Universal Video Provider abstraction lets Kinora
drive *any* text/image/reference-to-video model — a second hosted region, a
self-hosted lane, MiniMax, a future model — by making every provider declare its
own envelope up front. The Scheduler/Generator then asks the
:class:`~app.video.abstraction.registry.ProviderRegistry` for "a provider that
can do reference_to_video at 720p for 5s" instead of hard-coding Wan ids.

A :class:`VideoCapability` is a *pure, frozen* description (no I/O, no network):
which modes a provider supports, the duration window, the allowed resolutions /
aspect ratios / fps, whether it honours a seed or a negative prompt, how it wants
reference images conditioned, whether it emits audio, the max prompt length, and
whether it is async (submit→poll) or synchronous. :meth:`VideoCapability.supports`
answers a structured :class:`CapabilityQuery` against this envelope so routing is
a deterministic, testable predicate rather than a pile of ``if`` branches.

References:
    * §9.2 Phase B — render a shot (the seam this envelope gates).
    * §9.3 The Wan-mode decision tree (the modes enumerated here).
    * §11 budget — duration windows feed the video-seconds reservation.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------- #
# Canonical enums (provider-neutral)
# --------------------------------------------------------------------------- #


class VideoMode(StrEnum):
    """A provider-neutral render mode.

    Mirrors :class:`app.providers.types.WanMode` value-for-value so the
    :class:`~app.video.abstraction.normalizer.Normalizer` round-trips losslessly,
    but lives in the abstraction layer so a non-Wan provider need never import the
    Wan types. These are exactly the §9.3 decision-tree modes plus the
    instruction-edit lane.
    """

    #: Establishing shots with no character to lock (§9.3, "HappyHorse / t2v").
    TEXT_TO_VIDEO = "text_to_video"
    #: A single driving / start frame animated forward.
    IMAGE_TO_VIDEO = "image_to_video"
    #: Appearance (+ optional voice) pinned to locked references — kills face-drift.
    REFERENCE_TO_VIDEO = "reference_to_video"
    #: Storyboard → storyboard: land on an exact composition; fixed-length.
    FIRST_LAST_FRAME = "first_last_frame"
    #: Extend only from a QA-passed endpoint frame/clip (disciplined frame-chaining).
    VIDEO_CONTINUATION = "video_continuation"
    #: Minor change to an accepted clip ("make the coat red") without full regen.
    INSTRUCTION_EDIT = "instruction_edit"


class ReferenceStyle(StrEnum):
    """How a provider wants reference / conditioning images delivered.

    The abstraction is URL-or-bytes agnostic at the schema level; this only
    declares the *shape* a provider's native request expects so the normalizer
    can build it. Routing never depends on this — it is documentation + a hint to
    the adapter, surfaced for completeness.
    """

    #: No reference-image conditioning at all (pure t2v model).
    NONE = "none"
    #: A single image field (legacy Wan ``img_url`` / i2v first frame).
    SINGLE_IMAGE = "single_image"
    #: An ordered list of reference images (r2v identity locks).
    MULTI_IMAGE = "multi_image"
    #: A typed media array (Wan 2.7 ``input.media`` with ``first_frame`` etc.).
    TYPED_MEDIA = "typed_media"


class SubmitStyle(StrEnum):
    """Whether a render is submit→poll (async) or returns inline (sync)."""

    #: ``submit`` returns a handle; ``poll`` is required until terminal (Wan/DashScope).
    ASYNC_POLL = "async_poll"
    #: ``submit`` blocks and returns the finished result; ``poll`` is a no-op echo.
    SYNCHRONOUS = "synchronous"


# A neutral default catalogue of resolutions/aspect ratios most hosted families
# accept. Providers override with their own exact sets; these exist so a minimal
# capability still answers queries sensibly.
_DEFAULT_RESOLUTIONS: tuple[str, ...] = ("480P", "720P", "1080P")
_DEFAULT_ASPECTS: tuple[str, ...] = ("16:9", "9:16", "1:1")
_DEFAULT_FPS: tuple[int, ...] = (16, 24, 30)


def normalize_resolution(value: str) -> str:
    """Canonicalise a resolution label for case-insensitive comparison.

    ``"720p"``, ``"720P"`` and ``" 720P "`` all normalise to ``"720P"``. Pixel
    pairs like ``"1280x720"`` are upper-cased and stripped but otherwise kept
    verbatim (providers that speak pixel dimensions match on the exact string).
    """
    return value.strip().upper()


def normalize_aspect(value: str) -> str:
    """Canonicalise an aspect-ratio label (strip whitespace; keep the ``W:H``)."""
    return value.strip()


# --------------------------------------------------------------------------- #
# Capability query
# --------------------------------------------------------------------------- #


class CapabilityQuery(BaseModel):
    """A structured "can a provider do this?" question for the registry.

    Every field is optional: an empty query matches every provider. A populated
    field is a *constraint* the candidate's :class:`VideoCapability` must satisfy.
    This is the typed form of the task's example query — "find a provider that
    supports r2v at 720p 5s" — and keeps routing deterministic and unit-testable.

    Attributes:
        mode: required render mode.
        duration_s: a clip length that must fall inside ``[min, max]_duration_s``.
        resolution: a resolution label the provider must allow (case-insensitive).
        aspect_ratio: an aspect ratio the provider must allow.
        fps: a frame rate the provider must allow.
        needs_seed: provider must support a deterministic seed.
        needs_negative_prompt: provider must support a negative prompt.
        needs_audio: provider must emit an audio track.
        needs_async: ``True`` requires async submit→poll; ``False`` requires
            synchronous; ``None`` (default) accepts either.
        prompt_length: a prompt char-length the provider must accept.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: VideoMode | None = None
    duration_s: float | None = Field(default=None, gt=0)
    resolution: str | None = None
    aspect_ratio: str | None = None
    fps: int | None = Field(default=None, gt=0)
    needs_seed: bool = False
    needs_negative_prompt: bool = False
    needs_audio: bool = False
    needs_async: bool | None = None
    prompt_length: int | None = Field(default=None, ge=0)


# --------------------------------------------------------------------------- #
# The capability envelope
# --------------------------------------------------------------------------- #


class VideoCapability(BaseModel):
    """The full, declared envelope of a single video-gen provider.

    Frozen and pure — a value object the provider returns from
    :meth:`~app.video.abstraction.provider.UniversalVideoProvider.capabilities`.
    The registry indexes these to answer :class:`CapabilityQuery` lookups, and
    the normalizer reads ``default_resolution`` / ``default_fps`` /
    ``max_duration_s`` to fill request gaps.

    Validation enforces internal consistency (e.g. ``min_duration_s`` ≤
    ``max_duration_s``, every default appears in its allowed set), so a malformed
    capability fails loudly at construction rather than silently misrouting later.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Stable provider id (the registry key + telemetry label), e.g. ``"wan-hosted"``.
    provider_id: str = Field(min_length=1)
    #: Human-readable model/family description for logs and the picker UI.
    display_name: str = ""

    #: Render modes this provider can execute.
    modes: frozenset[VideoMode] = Field(default_factory=frozenset)

    #: Inclusive clip-duration window in seconds.
    min_duration_s: float = Field(default=1.0, gt=0)
    max_duration_s: float = Field(default=5.0, gt=0)
    #: Durations the provider snaps to (empty == any value in the window).
    discrete_durations_s: tuple[float, ...] = ()

    #: Allowed resolution labels (case-insensitive on query).
    resolutions: tuple[str, ...] = _DEFAULT_RESOLUTIONS
    #: Allowed aspect ratios.
    aspect_ratios: tuple[str, ...] = _DEFAULT_ASPECTS
    #: Allowed frame rates.
    fps_options: tuple[int, ...] = _DEFAULT_FPS

    #: Preferred defaults used to fill a request that left them unset.
    default_resolution: str | None = None
    default_aspect_ratio: str | None = None
    default_fps: int | None = None

    supports_seed: bool = True
    supports_negative_prompt: bool = True
    #: How the provider wants reference images delivered (documentation/hint).
    reference_style: ReferenceStyle = ReferenceStyle.SINGLE_IMAGE
    #: Max number of reference images accepted (0 == none; r2v models > 1).
    max_reference_images: int = Field(default=1, ge=0)
    #: Whether the provider produces an audio track alongside the clip.
    supports_audio: bool = False
    #: Max prompt length in characters (None == unbounded / not declared).
    max_prompt_chars: int | None = Field(default=None, gt=0)
    #: Submit style — async submit→poll vs. synchronous (sync providers may still
    #: be polled; their :meth:`poll` returns the already-terminal result).
    submit_style: SubmitStyle = SubmitStyle.ASYNC_POLL
    #: Whether the provider can cancel an in-flight task (some regions cannot).
    supports_cancel: bool = True

    #: Free-form provider tags for non-query routing hints (e.g. ``"turbo"``,
    #: ``"quality"``, ``"hosted"``) — opaque to the registry's structured query.
    tags: frozenset[str] = Field(default_factory=frozenset)

    # -- validation ------------------------------------------------------- #

    @model_validator(mode="after")
    def _check_internal_consistency(self) -> VideoCapability:
        if self.min_duration_s > self.max_duration_s:
            raise ValueError(
                f"min_duration_s ({self.min_duration_s}) > "
                f"max_duration_s ({self.max_duration_s})"
            )
        res = {normalize_resolution(r) for r in self.resolutions}
        asp = {normalize_aspect(a) for a in self.aspect_ratios}
        if (
            self.default_resolution is not None
            and normalize_resolution(self.default_resolution) not in res
        ):
            raise ValueError(
                f"default_resolution {self.default_resolution!r} is not in resolutions"
            )
        if (
            self.default_aspect_ratio is not None
            and normalize_aspect(self.default_aspect_ratio) not in asp
        ):
            raise ValueError(
                f"default_aspect_ratio {self.default_aspect_ratio!r} is not in aspect_ratios"
            )
        if self.default_fps is not None and self.default_fps not in self.fps_options:
            raise ValueError(f"default_fps {self.default_fps} is not in fps_options")
        for d in self.discrete_durations_s:
            if not (self.min_duration_s <= d <= self.max_duration_s):
                raise ValueError(
                    f"discrete duration {d}s falls outside the "
                    f"[{self.min_duration_s}, {self.max_duration_s}]s window"
                )
        if self.reference_style is ReferenceStyle.NONE and self.max_reference_images:
            raise ValueError(
                "reference_style=none but max_reference_images > 0 "
                "(declare MULTI_IMAGE/SINGLE_IMAGE/TYPED_MEDIA instead)"
            )
        return self

    # -- membership helpers (case-insensitive where appropriate) --------- #

    def supports_mode(self, mode: VideoMode) -> bool:
        """True iff this provider can execute ``mode``."""
        return mode in self.modes

    def supports_resolution(self, resolution: str) -> bool:
        """True iff ``resolution`` is allowed (case-insensitive)."""
        target = normalize_resolution(resolution)
        return any(normalize_resolution(r) == target for r in self.resolutions)

    def supports_aspect_ratio(self, aspect_ratio: str) -> bool:
        """True iff ``aspect_ratio`` is allowed."""
        target = normalize_aspect(aspect_ratio)
        return any(normalize_aspect(a) == target for a in self.aspect_ratios)

    def supports_fps(self, fps: int) -> bool:
        """True iff ``fps`` is an allowed frame rate."""
        return fps in self.fps_options

    def allows_duration(self, duration_s: float) -> bool:
        """True iff ``duration_s`` is renderable.

        A clip must fall inside the ``[min, max]`` window; if the provider snaps
        to ``discrete_durations_s`` it must additionally match one of those values
        (within a hair of float tolerance).
        """
        if not (self.min_duration_s <= duration_s <= self.max_duration_s):
            return False
        if self.discrete_durations_s:
            return any(abs(duration_s - d) <= 1e-6 for d in self.discrete_durations_s)
        return True

    def snap_duration(self, duration_s: float) -> float:
        """Snap ``duration_s`` to the nearest renderable value.

        Clamps into the window, then — for a discrete-duration provider — rounds
        to the closest allowed step (ties resolve to the shorter step, conserving
        scarce video-seconds, §11). Pure; never raises.
        """
        clamped = min(self.max_duration_s, max(self.min_duration_s, duration_s))
        if not self.discrete_durations_s:
            return clamped
        return min(self.discrete_durations_s, key=lambda d: (abs(d - clamped), d))

    # -- the query predicate --------------------------------------------- #

    def supports(self, query: CapabilityQuery) -> bool:
        """Deterministically answer whether this provider satisfies ``query``.

        Every populated field of ``query`` is an ``AND`` constraint. An empty
        query matches. This is the single predicate the registry uses to filter
        candidates, so routing logic stays out of the registry itself.
        """
        if query.mode is not None and not self.supports_mode(query.mode):
            return False
        if query.duration_s is not None and not self.allows_duration(query.duration_s):
            return False
        if query.resolution is not None and not self.supports_resolution(query.resolution):
            return False
        if query.aspect_ratio is not None and not self.supports_aspect_ratio(query.aspect_ratio):
            return False
        if query.fps is not None and not self.supports_fps(query.fps):
            return False
        if query.needs_seed and not self.supports_seed:
            return False
        if query.needs_negative_prompt and not self.supports_negative_prompt:
            return False
        if query.needs_audio and not self.supports_audio:
            return False
        if query.needs_async is not None:
            is_async = self.submit_style is SubmitStyle.ASYNC_POLL
            if query.needs_async != is_async:
                return False
        return not (
            query.prompt_length is not None
            and self.max_prompt_chars is not None
            and query.prompt_length > self.max_prompt_chars
        )


__all__ = [
    "CapabilityQuery",
    "ReferenceStyle",
    "SubmitStyle",
    "VideoCapability",
    "VideoMode",
    "normalize_aspect",
    "normalize_resolution",
]
