"""Output normalization + post-processing so clips from ANY provider are
interchangeable downstream.

Different video models emit different codecs / containers / fps / resolutions /
colour spaces / loudness. This package transcodes any provider clip into one
**canonical, stitch-ready** shape so :mod:`app.render.pipeline` /
:mod:`app.render.stitch` no longer carry provider-specific assumptions — a Wan
clip, a MiniMax (Hailuo) clip, and a degraded Ken-Burns rung all become drop-in
interchangeable.

Layers (each importable on its own):

* :mod:`~app.video.normalize.media_info` — typed :class:`MediaInfo` /
  :class:`StreamInfo` and a **pure** ``ffprobe`` JSON parser.
* :mod:`~app.video.normalize.targets` — the declarative
  :class:`NormalizationTarget` (geometry, aspect strategy, fps, codec/pixfmt,
  colour tags, loudness, focal point), built from settings or by hand.
* :mod:`~app.video.normalize.aspect` — pure letterbox / pillarbox / focal-point
  crop geometry math.
* :mod:`~app.video.normalize.plan` — the **pure** plan layer: build the exact
  ffmpeg arg lists (normalize / last-frame / concat) with no subprocess, fully
  unit-testable.
* :mod:`~app.video.normalize.probe` — :class:`ClipProbe`, the ffprobe wrapper
  (with an ``ffmpeg -i`` stderr fallback) → :class:`MediaInfo`.
* :mod:`~app.video.normalize.normalizer` — :class:`Normalizer`, the thin executor
  that transcodes one clip to the canonical target.
* :mod:`~app.video.normalize.lastframe` — the universal last-frame extractor for
  the image-to-video continuation handoff.
* :mod:`~app.video.normalize.concat` — concatenation with a uniform-input
  stream-copy fast path + a safe normalise-then-re-encode fallback.

ffmpeg/ffprobe are resolved portably (``KINORA_FFMPEG`` env > system >
``imageio-ffmpeg`` bundle), exactly like :mod:`app.render.degrade`.
"""

from __future__ import annotations

from .aspect import CropFit, PadFit, plan_crop_fit, plan_pad_fit
from .concat import ConcatResult, concat_clips, concat_clips_async
from .lastframe import extract_last_frame, extract_last_frame_async
from .media_info import MediaInfo, StreamInfo, parse_ffprobe_json, parse_rational
from .normalizer import Normalizer, NormalizeResult, is_normalize_error
from .plan import (
    ConcatPlan,
    NormalizePlan,
    VideoFilterPlan,
    build_concat_demux_args,
    build_concat_reencode_args,
    build_last_frame_args,
    build_normalize_args,
    build_video_filter,
    streams_are_uniform,
)
from .probe import ClipProbe
from .runtime import NormalizeError, ffmpeg_available, ffprobe_available
from .targets import (
    AspectStrategy,
    ColorTags,
    FocalPoint,
    LoudnessTarget,
    NormalizationTarget,
)

__all__ = [
    "AspectStrategy",
    "ClipProbe",
    "ColorTags",
    "ConcatPlan",
    "ConcatResult",
    "CropFit",
    "FocalPoint",
    "LoudnessTarget",
    "MediaInfo",
    "NormalizationTarget",
    "NormalizeError",
    "NormalizePlan",
    "NormalizeResult",
    "Normalizer",
    "PadFit",
    "StreamInfo",
    "VideoFilterPlan",
    "build_concat_demux_args",
    "build_concat_reencode_args",
    "build_last_frame_args",
    "build_normalize_args",
    "build_video_filter",
    "concat_clips",
    "concat_clips_async",
    "extract_last_frame",
    "extract_last_frame_async",
    "ffmpeg_available",
    "ffprobe_available",
    "is_normalize_error",
    "parse_ffprobe_json",
    "parse_rational",
    "plan_crop_fit",
    "plan_pad_fit",
    "streams_are_uniform",
]
