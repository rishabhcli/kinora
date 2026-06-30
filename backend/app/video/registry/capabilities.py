"""Capability vocabulary for the video-provider catalog (self-owned, sibling-free).

This module defines the *minimal* capability primitives the registry needs to
reason about which video model can serve a given render request — **without**
importing :mod:`app.providers.types`. Keeping the vocabulary local means this
subsystem never hard-blocks on a sibling agent reshaping ``WanMode`` /
``WanSpec``; the canonical short aliases (``t2v`` / ``i2v`` / ``r2v`` …) are the
contract the introspection API speaks.

Three primitives:

* :class:`VideoMode` — the render *shape* a model supports (text→video,
  image→video, reference→video, first-last-frame, video-continuation,
  instruction-edit). Short, stable string values so they round-trip cleanly
  through JSON / query params.
* :class:`Resolution` — an ordered ladder (``480P`` < ``720P`` < ``1080P`` …)
  with a numeric height key so "at least 720P" is a pure comparison.
* :class:`CapabilityProfile` — the declarative envelope of what *one* provider
  can do: which modes, which resolutions, the duration window, and a few coarse
  feature flags (audio, seed control, negative prompts). It owns the single
  :meth:`CapabilityProfile.satisfies` predicate every capability query funnels
  through, so "can this provider serve mode=r2v, 720P, ≥8s?" is one call.

Everything here is pure, deterministic, and import-cheap (no network, no DB, no
settings read), which is what lets the catalog parse and the queries run under a
plain :class:`~fastapi.testclient.TestClient`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class VideoMode(StrEnum):
    """A render *shape* a video model can produce.

    The values are the short, JSON/query-safe aliases the introspection API
    speaks. :meth:`coerce` accepts the long ``WanMode``-style spellings (e.g.
    ``"text_to_video"``) and a couple of common synonyms so a catalog authored
    against either vocabulary parses, and ``GET /video/capabilities?mode=t2v``
    and ``?mode=text_to_video`` resolve to the same thing.
    """

    T2V = "t2v"  # text → video
    I2V = "i2v"  # image → video
    R2V = "r2v"  # reference image(s) → video (locked identity)
    FLF = "flf"  # first + last frame → video
    CONTINUATION = "continuation"  # extend a prior clip
    EDIT = "edit"  # instruction-driven edit of a source clip

    @classmethod
    def coerce(cls, value: VideoMode | str) -> VideoMode:
        """Resolve a short alias, a long ``WanMode`` spelling, or a synonym.

        Raises:
            ValueError: the value matches no known mode (the message lists the
                accepted short aliases so a bad catalog/query is actionable).
        """
        if isinstance(value, cls):
            return value
        key = str(value).strip().lower()
        if key in _MODE_ALIASES:
            return _MODE_ALIASES[key]
        accepted = ", ".join(m.value for m in cls)
        raise ValueError(f"unknown video mode {value!r}; accepted: {accepted}")


#: Long-form / synonym spellings → canonical :class:`VideoMode`. Lets a catalog
#: authored against ``WanMode`` ("text_to_video") parse unchanged.
_MODE_ALIASES: dict[str, VideoMode] = {
    # canonical short aliases
    "t2v": VideoMode.T2V,
    "i2v": VideoMode.I2V,
    "r2v": VideoMode.R2V,
    "flf": VideoMode.FLF,
    "continuation": VideoMode.CONTINUATION,
    "edit": VideoMode.EDIT,
    # WanMode long spellings (app.providers.types.WanMode)
    "text_to_video": VideoMode.T2V,
    "image_to_video": VideoMode.I2V,
    "reference_to_video": VideoMode.R2V,
    "first_last_frame": VideoMode.FLF,
    "video_continuation": VideoMode.CONTINUATION,
    "instruction_edit": VideoMode.EDIT,
    # common synonyms
    "text2video": VideoMode.T2V,
    "image2video": VideoMode.I2V,
    "ref2video": VideoMode.R2V,
    "reference": VideoMode.R2V,
    "extend": VideoMode.CONTINUATION,
    "continue": VideoMode.CONTINUATION,
}


class Resolution(StrEnum):
    """A supported output resolution, ordered by pixel height.

    Stored as the provider-facing label (``"720P"``); :attr:`height` exposes the
    numeric key so "at least 720P" is ``a.height >= b.height`` — a total order
    that makes :meth:`CapabilityProfile.satisfies` a pure comparison.
    """

    SD_480 = "480P"
    HD_720 = "720P"
    HD_768 = "768P"  # MiniMax (Hailuo) native rung
    FHD_1080 = "1080P"
    QHD_1440 = "1440P"
    UHD_2160 = "2160P"

    @property
    def height(self) -> int:
        """The vertical pixel count this label denotes (the ordering key)."""
        return _RESOLUTION_HEIGHT[self]

    @classmethod
    def coerce(cls, value: Resolution | str) -> Resolution:
        """Resolve a label tolerantly (``"720p"``, ``" 720P "``, ``"720"``).

        Raises:
            ValueError: the value matches no rung on the ladder.
        """
        if isinstance(value, cls):
            return value
        key = str(value).strip().upper()
        if not key.endswith("P"):
            key = f"{key}P"
        for member in cls:
            if member.value == key:
                return member
        accepted = ", ".join(m.value for m in cls)
        raise ValueError(f"unknown resolution {value!r}; accepted: {accepted}")


_RESOLUTION_HEIGHT: dict[Resolution, int] = {
    Resolution.SD_480: 480,
    Resolution.HD_720: 720,
    Resolution.HD_768: 768,
    Resolution.FHD_1080: 1080,
    Resolution.QHD_1440: 1440,
    Resolution.UHD_2160: 2160,
}


class CapabilityProfile(BaseModel):
    """The declarative envelope of what one video provider can do.

    A provider can serve a request iff the requested mode is in :attr:`modes`,
    the requested resolution is in :attr:`resolutions`, and the requested
    duration falls within ``[min_duration_s, max_duration_s]``. The coarse
    feature flags (audio / seed / negative-prompt) are *advertised* capabilities
    surfaced by the introspection API; :meth:`satisfies` can optionally gate on
    them but does not by default (a query rarely needs to).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    modes: frozenset[VideoMode] = Field(
        ..., min_length=1, description="Render shapes this provider supports."
    )
    resolutions: frozenset[Resolution] = Field(
        ..., min_length=1, description="Output resolutions this provider supports."
    )
    min_duration_s: float = Field(
        1.0, gt=0, description="Shortest clip (seconds) the provider will render."
    )
    max_duration_s: float = Field(
        5.0, gt=0, description="Longest clip (seconds) the provider will render."
    )
    max_fps: int = Field(24, gt=0, le=120, description="Max output frame rate.")
    supports_audio: bool = Field(
        False, description="Emits a synchronized audio track (vs. silent video)."
    )
    supports_seed: bool = Field(
        True, description="Honors a deterministic seed for reproducible renders."
    )
    supports_negative_prompt: bool = Field(
        True, description="Accepts a negative prompt to steer away from content."
    )

    @field_validator("modes", mode="before")
    @classmethod
    def _coerce_modes(cls, value: object) -> object:
        """Coerce a list of short/long mode spellings into :class:`VideoMode`."""
        if isinstance(value, (str, VideoMode)):
            value = [value]
        if isinstance(value, (list, tuple, set, frozenset)):
            return frozenset(VideoMode.coerce(v) for v in value)
        return value

    @field_validator("resolutions", mode="before")
    @classmethod
    def _coerce_resolutions(cls, value: object) -> object:
        """Coerce a list of resolution labels into :class:`Resolution`."""
        if isinstance(value, (str, Resolution)):
            value = [value]
        if isinstance(value, (list, tuple, set, frozenset)):
            return frozenset(Resolution.coerce(v) for v in value)
        return value

    @model_validator(mode="after")
    def _check_duration_window(self) -> CapabilityProfile:
        if self.min_duration_s > self.max_duration_s:
            raise ValueError(
                "min_duration_s "
                f"({self.min_duration_s}) must not exceed max_duration_s "
                f"({self.max_duration_s})"
            )
        return self

    @property
    def max_resolution(self) -> Resolution:
        """The highest resolution rung this provider can reach."""
        return max(self.resolutions, key=lambda r: r.height)

    def satisfies(
        self,
        *,
        mode: VideoMode | str | None = None,
        duration_s: float | None = None,
        resolution: Resolution | str | None = None,
        require_audio: bool = False,
    ) -> bool:
        """Whether this profile can serve the (partial) request.

        Every constraint is optional — an unspecified facet is "don't care", so
        ``satisfies()`` with no arguments is always ``True``. This is the single
        predicate every capability query routes through.

        Args:
            mode: required render shape, or ``None`` for any.
            duration_s: required clip length in seconds; must fall in the
                provider's ``[min, max]`` window. ``None`` for any.
            resolution: minimum acceptable resolution — the provider qualifies
                if it can reach *at least* this rung. ``None`` for any.
            require_audio: when ``True``, only audio-capable providers qualify.
        """
        if mode is not None and VideoMode.coerce(mode) not in self.modes:
            return False
        if duration_s is not None and not (
            self.min_duration_s <= duration_s <= self.max_duration_s
        ):
            return False
        if resolution is not None:
            want = Resolution.coerce(resolution)
            if self.max_resolution.height < want.height:
                return False
        return not (require_audio and not self.supports_audio)


__all__ = [
    "CapabilityProfile",
    "Resolution",
    "VideoMode",
]
