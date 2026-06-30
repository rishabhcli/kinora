"""The canonical, provider-agnostic description of *one render to be priced*.

Every provider quotes against its own request shape (Wan's :class:`WanSpec`,
MiniMax's submit JSON, a future HappyHorse t2v call). To let them compete on one
ruler the cost layer prices a single neutral object — :class:`VideoCostRequest` —
that captures only the *cost-bearing* dimensions: how many seconds, at what
resolution and frame rate, in which mode, at what time (for surge), and against
which book/session/scene (for capping). It deliberately carries none of the
prompt, image bytes, or seeds — those don't change the price and would only
couple the cost layer to a particular provider's API.

A small adapter (:func:`from_wan_spec`) builds one from the existing
:class:`~app.providers.types.WanSpec` so the router can price a real render
without the caller re-describing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the provider layer at module import time
    from app.providers.types import WanSpec


class VideoMode(StrEnum):
    """The cost-relevant render families (a superset-mapping of Wan modes).

    Pricing tiers sometimes differ by mode — image-conditioned renders can cost
    more than text-to-video on some providers — so mode is a first-class pricing
    dimension, even though many providers price it flat.
    """

    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"
    FIRST_LAST_FRAME = "first_last_frame"
    VIDEO_CONTINUATION = "video_continuation"
    INSTRUCTION_EDIT = "instruction_edit"


@dataclass(frozen=True, slots=True)
class VideoCostRequest:
    """A provider-agnostic, cost-bearing description of one clip to render.

    Attributes:
        duration_s: Requested clip length in seconds (the scarce resource).
        resolution: A normalized tier label (``"480P"`` / ``"720P"`` / ``"768P"``
            / ``"1080P"``). Providers map their own labels onto these tiers.
        fps: Frames-per-second, when a provider prices per-frame; else advisory.
        mode: The render family (see :class:`VideoMode`).
        priority: Free-form lane hint (e.g. ``"committed"`` / ``"speculative"``);
            a provider may apply a peak/priority multiplier to it.
        peak: Whether this render falls in a provider's surge/peak window. The
            estimator never *infers* peak from a wall clock — the caller (who owns
            the clock) sets it, keeping pricing pure and deterministic.
        book_id / session_id / scene_id: Cap-scope identifiers carried through to
            the ledger and enforcement so per-book / per-session caps can bind.
        shot_id: Idempotency / telemetry correlation; never affects price.
        metadata: Opaque provider-specific hints (ignored by generic pricing).
    """

    duration_s: float
    resolution: str = "720P"
    fps: int = 24
    mode: VideoMode = VideoMode.TEXT_TO_VIDEO
    priority: str = "committed"
    peak: bool = False
    book_id: str | None = None
    session_id: str | None = None
    scene_id: str | None = None
    shot_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.duration_s < 0:
            raise ValueError("duration_s must be non-negative")
        if self.fps <= 0:
            raise ValueError("fps must be positive")

    @property
    def resolution_tier(self) -> str:
        """The normalized, upper-cased resolution label used for tier lookup."""
        return self.resolution.upper()

    @property
    def frame_count(self) -> int:
        """Total billed frames (``round(duration_s * fps)``), for per-frame pricing."""
        return round(self.duration_s * self.fps)

    def with_duration(self, duration_s: float) -> VideoCostRequest:
        """Return a copy at a different duration (for degradation what-ifs)."""
        return replace(self, duration_s=duration_s)

    def with_resolution(self, resolution: str) -> VideoCostRequest:
        """Return a copy at a different resolution tier (for degradation what-ifs)."""
        return replace(self, resolution=resolution)


_WAN_TO_VIDEO_MODE = {
    "text_to_video": VideoMode.TEXT_TO_VIDEO,
    "image_to_video": VideoMode.IMAGE_TO_VIDEO,
    "reference_to_video": VideoMode.REFERENCE_TO_VIDEO,
    "first_last_frame": VideoMode.FIRST_LAST_FRAME,
    "video_continuation": VideoMode.VIDEO_CONTINUATION,
    "instruction_edit": VideoMode.INSTRUCTION_EDIT,
}


def from_wan_spec(
    spec: WanSpec,
    *,
    fps: int = 24,
    peak: bool = False,
    priority: str = "committed",
    session_id: str | None = None,
    scene_id: str | None = None,
    book_id: str | None = None,
) -> VideoCostRequest:
    """Build a :class:`VideoCostRequest` from an existing :class:`WanSpec`.

    The router already holds a fully-resolved ``WanSpec`` before it picks a
    backend; this lets it price that exact render across providers without the
    caller re-stating the cost dimensions. ``WanMode`` values map 1:1 onto
    :class:`VideoMode`.
    """
    mode_value = spec.mode.value if hasattr(spec.mode, "value") else str(spec.mode)
    return VideoCostRequest(
        duration_s=float(spec.duration_s),
        resolution=spec.resolution,
        fps=fps,
        mode=_WAN_TO_VIDEO_MODE.get(mode_value, VideoMode.TEXT_TO_VIDEO),
        priority=priority,
        peak=peak,
        book_id=book_id,
        session_id=session_id,
        scene_id=scene_id,
        shot_id=spec.shot_id,
    )


__all__ = [
    "VideoCostRequest",
    "VideoMode",
    "from_wan_spec",
]
