"""Re-time audio word timings onto the *actual* rendered video duration (§9.4).

The narration almost never lands at exactly the video's length, and the video the
model returns is routinely clamped or retimed away from the requested duration. If
the karaoke ran on the narration clock it would drift against the picture. These
helpers map audio-clock :class:`WordTiming` s onto the video clock:

* :func:`rescale_to_duration` — single clip: linearly stretch/compress so the last
  word ends exactly at the clip's real duration.
* :func:`rescale_across_segments` — the **multi-segment** case: one logical shot
  rendered as N chained provider clips (``ClipSegment`` s). The audio spans all of
  them, so timings are scaled onto the *summed* real duration — a word straddling a
  seam keeps its single continuous span (the seams are invisible to the timeline,
  which is exactly what the reading room wants).

Generalizes :func:`app.render.sync_map.rescale_word_timings` (single-clip only) to
the chained-segment world while preserving relative word spacing. Pure.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import ClipSegment, TimingLike, WordTiming, coerce_words


def _narration_span(words: Sequence[WordTiming]) -> float:
    """The narration's own length: the largest ``t_end`` (timings may overlap)."""
    return max((w.t_end for w in words), default=0.0)


def rescale_to_duration(
    words: Sequence[TimingLike],
    *,
    target_duration_s: float,
) -> list[WordTiming]:
    """Linearly rescale audio-clock word timings to a single clip's real duration.

    Every word time is multiplied by ``target / narration_span`` so the last word
    ends exactly at ``target_duration_s`` and the highlight stays locked to the
    picture. Relative spacing is preserved (only the global tempo changes). A
    zero/empty narration span or non-positive target returns the words unchanged
    (nothing to anchor to). ``estimated`` provenance is carried through.
    """
    items = coerce_words(words)
    span = _narration_span(items)
    if not items or target_duration_s <= 0.0 or span <= 0.0:
        return list(items)
    factor = target_duration_s / span
    return [
        w.model_copy(
            update={
                "t_start": round(w.t_start * factor, 3),
                "t_end": round(min(target_duration_s, w.t_end * factor), 3),
            }
        )
        for w in items
    ]


def total_segment_duration(segments: Sequence[ClipSegment]) -> float:
    """Summed real duration of a chained multi-segment shot."""
    return round(sum(seg.duration_s for seg in segments), 3)


def rescale_across_segments(
    words: Sequence[TimingLike],
    segments: Sequence[ClipSegment],
) -> list[WordTiming]:
    """Re-time audio timings onto N chained provider clips (multi-segment shot).

    One logical shot is rendered as several provider clips played back-to-back; the
    single narration track spans all of them. The audio timeline is rescaled onto
    the **summed** real duration of the segments, so a word that straddles a clip
    seam keeps one continuous span — the chained clips read as a single timeline,
    which is what the reading room paints. Falls back to returning the words
    unchanged when there are no segments.
    """
    items = coerce_words(words)
    if not segments:
        return list(items)
    return rescale_to_duration(items, target_duration_s=total_segment_duration(segments))


def segment_boundaries(segments: Sequence[ClipSegment]) -> list[float]:
    """Cumulative seam offsets on the video clock (``[0, d0, d0+d1, …, total]``).

    The first entry is always ``0.0`` and the last is the total duration; the
    interior values are where one chained clip ends and the next begins. Useful for
    a client that wants to know, per word, which physical clip it falls in.
    """
    bounds = [0.0]
    acc = 0.0
    for seg in segments:
        acc += seg.duration_s
        bounds.append(round(acc, 3))
    return bounds


def segment_index_at(boundaries: Sequence[float], t: float) -> int:
    """Index of the chained clip containing video-time ``t`` (clamped to range).

    ``boundaries`` is the output of :func:`segment_boundaries`. A time on a seam
    belongs to the *earlier* clip's successor (half-open ``[start, end)`` per clip,
    with the final clip closed at the end).
    """
    n = max(0, len(boundaries) - 1)
    if n == 0:
        return 0
    for i in range(n):
        if boundaries[i] <= t < boundaries[i + 1]:
            return i
    return n - 1 if t >= boundaries[-1] else 0


__all__ = [
    "rescale_across_segments",
    "rescale_to_duration",
    "segment_boundaries",
    "segment_index_at",
    "total_segment_duration",
]
