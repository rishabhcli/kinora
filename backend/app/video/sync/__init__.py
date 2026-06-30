"""Backend-agnostic sync-map layer (§9.4) — works for any video+audio backend.

The §9.4 sync map binds *video-time ↔ page ↔ word* (karaoke highlight, page-turn,
scroll⟷video seek). The original :mod:`app.render.sync_map` builder assumed the
hosted CosyVoice/Qwen ``word_timestamps`` shape and a single clip duration. This
package generalizes that so the sync map is correct regardless of which video and
audio backend produced the assets, by composing four stages:

1. :mod:`~app.video.sync.ingest` — normalize *any* provider timing shape (per-word,
   per-char, per-token, SRT/VTT cues, or none) into one canonical
   :class:`~app.video.sync.models.WordTiming` timeline;
2. :mod:`~app.video.sync.estimator` — forced-alignment fallback for backends that
   return no timings (distribute words across the clip by syllable + punctuation);
3. :mod:`~app.video.sync.retime` — rescale audio timings onto the *actual* rendered
   video duration, including the multi-segment chained-clip case;
4. :mod:`~app.video.sync.builder` — emit the §9.4 :class:`~app.video.sync.models.SyncMap`
   (karaoke spans + page-turn + per-sentence anchors), reusing the proven page-box
   alignment from :mod:`app.render.sync_map`.

Plus :mod:`~app.video.sync.validators` (monotonic / within-duration / full coverage)
and :mod:`~app.video.sync.export` (WebVTT / SRT). Everything is pure — no DB,
network, or ffmpeg — so it is exhaustively unit-testable and deterministic. This is
an **additive** layer beside the existing builder; it never rewrites it.
"""

from __future__ import annotations

from .builder import build_sync_map
from .estimator import estimate_word_timings
from .export import to_srt, to_webvtt
from .ingest import ingest_timings, sniff_shape, words_from_cue
from .models import (
    ClipSegment,
    RawCue,
    SyncMap,
    SyncSentence,
    SyncWord,
    TimingShape,
    WordTiming,
    coerce_word,
    coerce_words,
)
from .retime import (
    rescale_across_segments,
    rescale_to_duration,
    segment_boundaries,
    segment_index_at,
    total_segment_duration,
)
from .validators import (
    assert_valid_sync_map,
    check_coverage,
    check_monotonic,
    check_within_duration,
    validate_sync_map,
    validate_word_timeline,
)

__all__ = [
    "ClipSegment",
    "RawCue",
    "SyncMap",
    "SyncSentence",
    "SyncWord",
    "TimingShape",
    "WordTiming",
    "assert_valid_sync_map",
    "build_sync_map",
    "check_coverage",
    "check_monotonic",
    "check_within_duration",
    "coerce_word",
    "coerce_words",
    "estimate_word_timings",
    "ingest_timings",
    "rescale_across_segments",
    "rescale_to_duration",
    "segment_boundaries",
    "segment_index_at",
    "sniff_shape",
    "to_srt",
    "to_webvtt",
    "total_segment_duration",
    "validate_sync_map",
    "validate_word_timeline",
    "words_from_cue",
]
