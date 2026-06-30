"""Per-provider transcode profiles — normalize heterogeneous clips for seamless ABR.

Kinora's film is assembled from clips produced by **different models** — Wan
(DashScope) t2v/i2v, MiniMax, a local Wan2.x host server, and the offline
Ken-Burns degradation lane (``app.render.degrade``). Each emits its own codec,
frame rate, GOP structure, and keyframe cadence. For adaptive-bitrate streaming
to switch *seamlessly* between renditions — and for fMP4/CMAF segments to align
on shot boundaries — every clip must be normalized to one common grid:

* a single **codec** (H.264 high) and pixel format (yuv420p),
* a constant **frame rate** (the film's ``fps``, default 30),
* a **closed GOP** whose length divides evenly into the segment duration so an
  IDR frame lands exactly on every segment boundary (the prerequisite for ABR
  switching — a player can only switch renditions at an IDR boundary), and
* **forced keyframes** at the segment cadence (``-force_key_frames``).

A :class:`ProviderProfile` captures what a provider *natively* emits (so we know
how aggressively to re-encode — e.g. a clip that is already H.264/30 with a long
open GOP only needs a GOP fix, while a VP9/24fps clip needs a full transcode)
plus the *target* normalization knobs. :data:`PROVIDER_PROFILES` maps a provider
key to its profile; :func:`profile_for` resolves a key (with a permissive
fallback to a full-transcode default) so an unknown future provider still
normalizes correctly.

This module is **pure**: it only describes encode parameters. The plan layer
(:mod:`app.delivery.segmenter`) consumes a :class:`NormalizationSpec` to build
ffmpeg args; nothing here shells out.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.delivery.errors import ProfileError

#: The canonical streaming codec + pixel format. Every rung is H.264/yuv420p so
#: the RFC 6381 codec string is uniform and players never re-probe on switch.
TARGET_VIDEO_CODEC = "h264"
TARGET_PIXEL_FORMAT = "yuv420p"
TARGET_AUDIO_CODEC = "aac"
#: AAC sample rate the audio is resampled to (matches ``app.render.stitch._AUDIO_SR``
#: of 44100? — note: stitch uses 44100; streaming standardises on 48000, the
#: broadcast/CMAF norm, and the normalization step resamples to it).
TARGET_AUDIO_SAMPLE_RATE = 48000


class ProviderProfile(BaseModel):
    """What a provider natively emits + how to normalize it for ABR streaming."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Stable key (e.g. ``"wan_dashscope"``, ``"minimax"``, ``"ken_burns"``).
    key: str = Field(min_length=1)
    #: Human label for logs/manifests.
    label: str = Field(min_length=1)
    #: The codec the provider natively emits, lower-cased (``"h264"``, ``"hevc"``,
    #: ``"vp9"``, …). ``None`` = unknown (assume a full transcode is needed).
    native_codec: str | None = None
    #: The provider's native frame rate, if fixed/known (Wan turbo ≈ 16, MiniMax
    #: ≈ 25, Ken-Burns = 30). ``None`` = variable/unknown.
    native_fps: float | None = None
    #: Whether the provider's GOP is already closed (rare for generative video).
    native_closed_gop: bool = False
    #: True when the clip can be **stream-copied** for the *top* rung (codec/fps
    #: already match the target and the GOP just needs re-stamping). Generative
    #: providers set this False — their GOP/keyframe cadence is unreliable, so we
    #: always re-encode to guarantee IDR-on-boundary.
    copy_safe: bool = False
    #: Extra notes surfaced in DESIGN/telemetry.
    notes: str = ""

    @property
    def needs_full_transcode(self) -> bool:
        """True when the native stream must be fully re-encoded to the target codec."""
        return self.native_codec != TARGET_VIDEO_CODEC or not self.copy_safe


#: The provider profile registry. Keys are matched case-insensitively by
#: :func:`profile_for`, with substring fallbacks so e.g. ``"wan2.1-i2v-turbo"``
#: resolves to the Wan profile.
PROVIDER_PROFILES: Mapping[str, ProviderProfile] = {
    "wan_dashscope": ProviderProfile(
        key="wan_dashscope",
        label="Wan (DashScope intl)",
        native_codec="h264",
        native_fps=16.0,
        native_closed_gop=False,
        copy_safe=False,
        notes="Hosted Wan turbo/plus; ~16fps, open GOP — re-encode to 30fps closed GOP.",
    ),
    "wan_local": ProviderProfile(
        key="wan_local",
        label="Wan2.x local host (MPS)",
        native_codec="h264",
        native_fps=16.0,
        native_closed_gop=False,
        copy_safe=False,
        notes="Local TI2V host; rough tester quality, re-encode for cadence.",
    ),
    "minimax": ProviderProfile(
        key="minimax",
        label="MiniMax video",
        native_codec="h264",
        native_fps=25.0,
        native_closed_gop=False,
        copy_safe=False,
        notes="MiniMax ~25fps; conform to film fps + segment-aligned IDR.",
    ),
    "ken_burns": ProviderProfile(
        key="ken_burns",
        label="Ken-Burns degradation lane",
        native_codec="h264",
        native_fps=30.0,
        native_closed_gop=False,
        copy_safe=False,
        notes="Offline ffmpeg still+pan (app.render.degrade); already 30fps/yuv420p.",
    ),
    "unknown": ProviderProfile(
        key="unknown",
        label="Unknown provider",
        native_codec=None,
        native_fps=None,
        native_closed_gop=False,
        copy_safe=False,
        notes="Permissive default: full transcode to the target grid.",
    ),
}

#: The profile used when no key matches — forces a full normalize.
DEFAULT_PROFILE_KEY = "unknown"


def profile_for(provider: str | None) -> ProviderProfile:
    """Resolve a provider key to its :class:`ProviderProfile`.

    Matching is forgiving: an exact (case-insensitive) key first, then a
    substring match against known keys *and* their model-id families (so
    ``"wan2.1-i2v-turbo"`` → ``wan_dashscope``, ``"minimax-hailuo"`` →
    ``minimax``). An unrecognised / ``None`` provider resolves to the permissive
    full-transcode default rather than raising — a new model must still stream.
    """
    if not provider:
        return PROVIDER_PROFILES[DEFAULT_PROFILE_KEY]
    key = provider.strip().lower()
    if key in PROVIDER_PROFILES:
        return PROVIDER_PROFILES[key]
    # Model-id family heuristics.
    if "wan" in key:
        return PROVIDER_PROFILES["wan_local" if "local" in key else "wan_dashscope"]
    if "minimax" in key or "hailuo" in key:
        return PROVIDER_PROFILES["minimax"]
    if "ken" in key or "burns" in key or "degrade" in key:
        return PROVIDER_PROFILES["ken_burns"]
    return PROVIDER_PROFILES[DEFAULT_PROFILE_KEY]


class NormalizationSpec(BaseModel):
    """The concrete normalization target a clip must be conformed to.

    Derived from a :class:`ProviderProfile` + the stream's ``fps`` and
    ``segment_duration_s`` by :func:`normalization_spec`. It carries everything
    the plan layer needs to emit ffmpeg args that guarantee **IDR frames land on
    every segment boundary** (the precondition for seamless ABR switching).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_key: str
    target_codec: str = TARGET_VIDEO_CODEC
    pixel_format: str = TARGET_PIXEL_FORMAT
    audio_codec: str = TARGET_AUDIO_CODEC
    audio_sample_rate: int = TARGET_AUDIO_SAMPLE_RATE
    fps: int = Field(gt=0)
    segment_duration_s: float = Field(gt=0)
    #: GOP length in frames = ``fps * segment_duration_s`` (integer; the spec is
    #: rejected if that product is not an integer so IDRs align perfectly).
    gop_size: int = Field(gt=0)
    #: Whether a full re-encode is required (vs. a GOP-only fixup; generative
    #: clips are always full).
    full_transcode: bool = True
    #: Keyframe placement expression for ``-force_key_frames`` (segment cadence).
    force_keyframe_expr: str


def normalization_spec(
    profile: ProviderProfile, *, fps: int, segment_duration_s: float
) -> NormalizationSpec:
    """Build the :class:`NormalizationSpec` that aligns IDRs to segment boundaries.

    The GOP length is ``fps * segment_duration_s`` and **must be an integer** —
    otherwise keyframes drift off segment boundaries and ABR switching produces
    visible glitches. This is enforced here (the segment duration is chosen by
    the manifest layer precisely so this holds, e.g. 2s @ 30fps = 60-frame GOP).

    Raises:
        ProfileError: if ``fps * segment_duration_s`` is not (near) integral.
    """
    if fps <= 0 or segment_duration_s <= 0:
        raise ProfileError("fps and segment_duration_s must be positive")
    gop_float = fps * segment_duration_s
    gop_size = round(gop_float)
    if not math.isclose(gop_float, gop_size, abs_tol=1e-6):
        raise ProfileError(
            f"fps*segment_duration must be integral for IDR alignment; "
            f"got {fps}*{segment_duration_s}={gop_float}"
        )
    # ``-force_key_frames expr:gte(t,n_forced*<seg>)`` forces a keyframe every
    # ``segment_duration_s`` seconds regardless of scene content.
    expr = f"expr:gte(t,n_forced*{segment_duration_s:g})"
    return NormalizationSpec(
        provider_key=profile.key,
        fps=fps,
        segment_duration_s=segment_duration_s,
        gop_size=gop_size,
        full_transcode=profile.needs_full_transcode,
        force_keyframe_expr=expr,
    )
