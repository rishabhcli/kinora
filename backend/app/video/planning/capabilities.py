"""Provider capability profiles — what a video backend can and cannot do.

The planner (:mod:`app.video.planning.planner`) is *capability-aware*: instead of
hard-coding what DashScope Wan, MiniMax Hailuo, or a future backend support, it
consumes a small, self-contained :class:`CapabilityProfile` that *declares* a
backend's envelope (which render modes, what duration / fps / resolution / aspect
ranges, seed support, prompt budget, how many reference images, …). Given the
declared profile and a desired canonical request, the planner translates and
degrades until the request fits — see :mod:`app.video.planning.planner`.

This abstraction is **owned by this package** on purpose: the marathon brief
forbids hard-depending on other agents' work, and a profile is a far better seam
than reaching into ``app.providers`` internals. A backend author writes one
:class:`CapabilityProfile` (or uses a preset here) and the planner does the rest.

Pure data + pure helpers — no network, no I/O, no env reads. Everything is a
frozen pydantic model so a profile is hashable-by-value, loggable, and trivially
fixture-able in tests.
"""

from __future__ import annotations

from enum import StrEnum
from fractions import Fraction
from math import gcd

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VideoMode(StrEnum):
    """The canonical render modes a request can ask for.

    Values mirror :class:`app.providers.types.WanMode` / the agents'
    :class:`app.agents.contracts.RenderMode` so a profile is interchangeable with
    those by value, but this package stays self-contained and imports neither — a
    backend that is not Wan-shaped still declares its support in these terms.
    """

    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"
    FIRST_LAST_FRAME = "first_last_frame"
    VIDEO_CONTINUATION = "video_continuation"
    INSTRUCTION_EDIT = "instruction_edit"


#: A canonical resolution token → (width, height) in pixels, long-side oriented as
#: landscape. Aspect for a token is derived from these so a profile only declares
#: tokens, never raw pixels.
_RESOLUTION_PIXELS: dict[str, tuple[int, int]] = {
    "240P": (426, 240),
    "360P": (640, 360),
    "480P": (854, 480),
    "540P": (960, 540),
    "576P": (1024, 576),
    "720P": (1280, 720),
    "768P": (1366, 768),
    "1080P": (1920, 1080),
    "1440P": (2560, 1440),
    "2160P": (3840, 2160),
    "4K": (3840, 2160),
}


def resolution_pixels(token: str) -> tuple[int, int] | None:
    """The landscape ``(width, height)`` pixel pair for a resolution token.

    Returns ``None`` for an unknown token. Case-insensitive; a bare ``"720"`` is
    treated as ``"720P"``.
    """
    key = token.strip().upper()
    if key and key[-1].isdigit():
        key = f"{key}P"
    return _RESOLUTION_PIXELS.get(key)


def resolution_height(token: str) -> int:
    """The vertical pixel count of a resolution token (0 when unknown).

    Used to order resolutions cheapest→richest without assuming a fixed aspect.
    """
    px = resolution_pixels(token)
    return px[1] if px else 0


class AspectRatio(BaseModel):
    """A width:height aspect ratio in lowest terms (e.g. ``16:9``, ``9:16``).

    Stored as integers so two ratios compare by value regardless of how they were
    constructed; :meth:`value` is the float used for nearest-match clamping.
    """

    model_config = ConfigDict(frozen=True)

    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @model_validator(mode="after")
    def _reduce(self) -> AspectRatio:
        g = gcd(self.width, self.height)
        if g > 1:
            object.__setattr__(self, "width", self.width // g)
            object.__setattr__(self, "height", self.height // g)
        return self

    @classmethod
    def parse(cls, text: str) -> AspectRatio:
        """Parse ``"16:9"`` / ``"16x9"`` / ``"1.78"`` into an :class:`AspectRatio`.

        Raises:
            ValueError: when the string is not a ratio or a positive decimal.
        """
        raw = text.strip().lower().replace("x", ":")
        if ":" in raw:
            w_str, _, h_str = raw.partition(":")
            return cls(width=int(w_str), height=int(h_str))
        frac = Fraction(raw).limit_denominator(1000)
        return cls(width=frac.numerator, height=frac.denominator)

    @classmethod
    def from_pixels(cls, width: int, height: int) -> AspectRatio:
        return cls(width=width, height=height)

    @property
    def value(self) -> float:
        return self.width / self.height

    @property
    def is_portrait(self) -> bool:
        return self.height > self.width

    @property
    def is_landscape(self) -> bool:
        return self.width > self.height

    @property
    def is_square(self) -> bool:
        return self.width == self.height

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.width}:{self.height}"


#: Common aspects, so callers don't repeat the integer pairs.
ASPECT_16_9 = AspectRatio(width=16, height=9)
ASPECT_9_16 = AspectRatio(width=9, height=16)
ASPECT_1_1 = AspectRatio(width=1, height=1)
ASPECT_4_3 = AspectRatio(width=4, height=3)
ASPECT_21_9 = AspectRatio(width=21, height=9)


class CapabilityProfile(BaseModel):
    """A declared description of one video backend's render envelope.

    The planner reads *only* this to decide how to translate/degrade a request.
    A field that is empty / ``None`` means "no constraint" where that is sensible
    (e.g. an empty ``fps_options`` means the backend accepts any fps), and "not
    supported" where a capability is boolean (e.g. ``supports_seed``).

    Attributes:
        name: Stable backend identity (for rationale + telemetry).
        modes: The set of natively-supported :class:`VideoMode` s. A mode *not*
            in here is reachable only by translation (synthesize-then-i2v, etc.).
        min_duration_s / max_duration_s: The single-call duration window. A longer
            request is split into chained continuation segments.
        duration_options: If non-empty, the *only* discrete durations the backend
            accepts (a request is snapped to the nearest one ≤ the window).
        fps_options: Allowed frame rates; empty = any. Out-of-set fps clamps to the
            nearest supported value.
        resolution_options: Allowed resolution tokens (see ``_RESOLUTION_PIXELS``);
            empty = any. Out-of-set clamps to the nearest by height.
        aspect_options: Allowed aspect ratios; empty = any. Out-of-set clamps to
            the nearest by float value (orientation-aware tie-break).
        supports_seed: Whether a deterministic seed can be pinned.
        max_reference_images: How many r2v reference images the backend accepts
            (0 = no reference-to-video at all even if the mode is listed).
        supports_negative_prompt: Whether a negative prompt is honoured.
        max_prompt_chars: Hard cap on prompt length; longer prompts are compressed.
        can_synthesize_keyframe: Whether this backend can produce a still keyframe
            (text→image) to feed its own i2v — i.e. it can self-bootstrap a
            reference. When ``False``, the planner must assume an *external*
            keyframe source is wired downstream (it still emits the synth step).
        supports_continuation_overlap: Whether chained segments can overlap a tail
            window for a clean stitch (vs. a hard cut at the boundary).
        overlap_s: The overlap window used between chained segments when supported.
    """

    model_config = ConfigDict(frozen=True)

    name: str = "backend"
    modes: frozenset[VideoMode] = Field(
        default_factory=lambda: frozenset({VideoMode.TEXT_TO_VIDEO})
    )

    min_duration_s: float = 1.0
    max_duration_s: float = 5.0
    duration_options: tuple[int, ...] = ()

    fps_options: tuple[int, ...] = ()
    resolution_options: tuple[str, ...] = ()
    aspect_options: tuple[AspectRatio, ...] = ()

    supports_seed: bool = True
    max_reference_images: int = 0
    supports_negative_prompt: bool = True
    max_prompt_chars: int = 2000

    can_synthesize_keyframe: bool = True
    supports_continuation_overlap: bool = True
    overlap_s: float = 0.5

    # -- capability queries (pure) --------------------------------------- #

    def supports_mode(self, mode: VideoMode) -> bool:
        """Whether ``mode`` is natively supported.

        Reference-to-video additionally requires at least one reference-image
        slot; a profile that lists the mode but declares zero slots does not in
        fact support it.
        """
        if mode is VideoMode.REFERENCE_TO_VIDEO and self.max_reference_images <= 0:
            return False
        return mode in self.modes

    def clamp_duration(self, duration_s: float) -> float:
        """Clamp a *single-segment* duration into the backend's window/options.

        Discrete ``duration_options`` win when present (snap to the nearest option
        that does not exceed the request, falling back to the smallest option);
        otherwise clamp to ``[min_duration_s, max_duration_s]``.
        """
        if self.duration_options:
            opts = sorted(self.duration_options)
            le = [o for o in opts if o <= duration_s + 1e-9]
            return float(le[-1]) if le else float(opts[0])
        return float(min(max(duration_s, self.min_duration_s), self.max_duration_s))

    def clamp_fps(self, fps: int) -> int:
        """Snap ``fps`` to the nearest supported value (any fps when unconstrained)."""
        if not self.fps_options:
            return fps
        return min(self.fps_options, key=lambda o: (abs(o - fps), o))

    def clamp_resolution(self, resolution: str) -> str:
        """Snap a resolution token to the nearest supported one by height.

        Unconstrained → returned unchanged. An unknown requested token (no pixel
        mapping) snaps to the *richest* supported option as a safe default.
        """
        if not self.resolution_options:
            return resolution
        want = resolution_height(resolution)
        if want == 0:
            return max(self.resolution_options, key=resolution_height)
        return min(
            self.resolution_options,
            key=lambda o: (abs(resolution_height(o) - want), -resolution_height(o)),
        )

    def clamp_aspect(self, aspect: AspectRatio) -> AspectRatio:
        """Snap an aspect to the nearest supported ratio (orientation-aware).

        Unconstrained → returned unchanged. Ties (equal float distance) prefer a
        candidate of the *same* orientation (portrait↔portrait), then the wider
        one, so a 9:16 request never silently flips to landscape when a portrait
        option exists.
        """
        if not self.aspect_options:
            return aspect
        want = aspect.value

        def key(o: AspectRatio) -> tuple[float, int, float]:
            same_orientation = (
                o.is_portrait == aspect.is_portrait and o.is_landscape == aspect.is_landscape
            )
            return (abs(o.value - want), 0 if same_orientation else 1, -o.value)

        return min(self.aspect_options, key=key)


__all__ = [
    "ASPECT_16_9",
    "ASPECT_1_1",
    "ASPECT_21_9",
    "ASPECT_4_3",
    "ASPECT_9_16",
    "AspectRatio",
    "CapabilityProfile",
    "VideoMode",
    "resolution_height",
    "resolution_pixels",
]
