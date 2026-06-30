"""Assemble a backend-agnostic :class:`SyncMap` for one shot (§9.4).

This is the top of the layer: it threads the stages together —

1. **ingest** any provider timing shape (or estimate from text) into a canonical
   :class:`WordTiming` timeline (:mod:`app.video.sync.ingest`);
2. **retime** the audio-clock timings onto the *actual* rendered video duration,
   single clip or N chained segments (:mod:`app.video.sync.retime`);
3. **align** narrated words to the page's word boxes for ``word_index`` + ``bbox``,
   reusing the proven pure helpers in :mod:`app.render.sync_map` so the geometry is
   identical to what the reading room already paints;
4. emit the §9.4 shape — karaoke spans + ``page_turn_at_s`` + per-sentence anchors.

It does **not** rewrite the existing :func:`app.render.sync_map.build_sync_segment`;
it sits beside it and delegates to its alignment primitives. The output
:class:`SyncMap` is field-compatible with ``SyncSegment`` (plus ``sentences`` /
``estimated``), so a caller can hand it straight to the reading room. Pure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.render.sync_map import (
    align_words,
    page_turn_at,
    source_words_in_span,
)

from .ingest import ingest_timings
from .models import (
    ClipSegment,
    SyncMap,
    SyncSentence,
    SyncWord,
    TimingShape,
    WordTiming,
    coerce_words,
)
from .retime import rescale_across_segments, rescale_to_duration
from .text import split_sentences, tokenize

#: §9.4 example: the page turns ~0.2s before a 5.0s shot ends.
_DEFAULT_PAGE_TURN_LEAD_S = 0.2


def _span_page(source_span: Mapping[str, Any] | None) -> int:
    if source_span is None:
        return 0
    page = source_span.get("page")
    return int(page) if page is not None else 0


def _span_range(source_span: Mapping[str, Any] | None) -> tuple[int, int] | None:
    if source_span is None:
        return None
    raw = source_span.get("word_range")
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return (int(raw[0]), int(raw[1]))
    return None


def _retime(
    words: Sequence[WordTiming],
    *,
    duration_s: float | None,
    segments: Sequence[ClipSegment] | None,
) -> tuple[list[WordTiming], float]:
    """Map audio-clock words onto the real video clock; return ``(words, duration)``.

    Segments (the multi-segment chained-clip case) win over a single ``duration_s``;
    when neither is given the timings are left on the narration clock and the
    duration is the last word's end.
    """
    items = coerce_words(words)
    if segments:
        retimed = rescale_across_segments(items, segments)
        total = round(sum(s.duration_s for s in segments), 3)
        return retimed, total
    if duration_s is not None and duration_s > 0.0:
        return rescale_to_duration(items, target_duration_s=duration_s), round(duration_s, 3)
    end = max((w.t_end for w in items), default=0.0)
    return items, round(end, 3)


def _build_sentences(words: Sequence[SyncWord], narration_text: str | None) -> list[SyncSentence]:
    """Group the karaoke words into per-sentence anchors (§9.4 sentence anchors).

    When ``narration_text`` is supplied, sentences are taken from its punctuation;
    otherwise they are inferred from the words' own trailing terminal punctuation.
    Each anchor's span is its words' ``[min t_start, max t_end]`` and it records the
    inclusive ``word_start``/``word_end`` slice. Returns ``[]`` for no words.
    """
    if not words:
        return []
    # Sentence sizes (in word counts), in order.
    sizes: list[int]
    if narration_text and narration_text.strip():
        sizes = [len(tokenize(s)) for s in split_sentences(narration_text)]
    else:
        sizes = _infer_sentence_sizes([w.text for w in words])
    # Reconcile the sizes with the actual word count (estimation can disagree by a
    # token or two): clamp, then sweep any remainder into a trailing sentence.
    sentences: list[SyncSentence] = []
    cursor = 0
    n = len(words)
    for size in sizes:
        if cursor >= n:
            break
        end = min(n - 1, cursor + max(1, size) - 1)
        sentences.append(_sentence_from_slice(words, cursor, end))
        cursor = end + 1
    if cursor < n:  # leftover words → one final anchor
        sentences.append(_sentence_from_slice(words, cursor, n - 1))
    return sentences


def _infer_sentence_sizes(texts: Sequence[str]) -> list[int]:
    """Word-count of each sentence, inferred from terminal punctuation per word."""
    sizes: list[int] = []
    count = 0
    for text in texts:
        count += 1
        stripped = text.rstrip("\"'”’)]")
        if stripped and stripped[-1] in ".!?…。！？":
            sizes.append(count)
            count = 0
    if count:
        sizes.append(count)
    return sizes


def _sentence_from_slice(words: Sequence[SyncWord], start: int, end: int) -> SyncSentence:
    chunk = words[start : end + 1]
    return SyncSentence(
        text=" ".join(w.text for w in chunk),
        t_start=round(min(w.t_start for w in chunk), 3),
        t_end=round(max(w.t_end for w in chunk), 3),
        word_start=start,
        word_end=end,
    )


def build_sync_map(
    *,
    shot_id: str,
    raw_timings: Sequence[Any] | None = None,
    timing_shape: TimingShape | None = None,
    narration_text: str | None = None,
    source_span: Mapping[str, Any] | None = None,
    page_word_boxes: Sequence[Mapping[str, Any]] | None = None,
    video_start_s: float = 0.0,
    duration_s: float | None = None,
    segments: Sequence[ClipSegment] | None = None,
    timing_unit: str = "s",
    page_turn_lead_s: float = _DEFAULT_PAGE_TURN_LEAD_S,
) -> SyncMap:
    """Build the §9.4 :class:`SyncMap` for one shot from *any* backend's output.

    Args:
        shot_id: the shot this map belongs to.
        raw_timings: the provider's raw timing entries (any :class:`TimingShape`),
            or ``None``/empty to force the estimator.
        timing_shape: the known shape; sniffed from ``raw_timings`` when omitted.
        narration_text: the spoken text — needed to *estimate* timings when there
            are none, and used (when present) to anchor per-sentence boundaries.
        source_span: ``{"page", "word_range": [start, end]}`` for the shot.
        page_word_boxes: the page's ``[{word_index, text, bbox}]`` (PyMuPDF).
        video_start_s: the shot's start on the scene playhead (0 for standalone).
        duration_s: the actual rendered clip duration (single clip). Ignored when
            ``segments`` is given.
        segments: chained provider clips for a multi-segment shot; their summed
            real duration is the retime target and seams are made invisible.
        timing_unit: ``"s"`` or ``"ms"`` for ``raw_timings``.
        page_turn_lead_s: how far before the shot end to flip the page.

    Returns:
        A :class:`SyncMap` whose word spans are on the scene playhead (offset by
        ``video_start_s``), with page-turn + per-sentence anchors and an
        ``estimated`` flag set when the timings were estimated.
    """
    # 1. Normalize provider timings → canonical audio-clock words (or estimate).
    target_for_estimate = round(sum(s.duration_s for s in segments), 3) if segments else duration_s
    audio_words = ingest_timings(
        raw_timings,
        shape=timing_shape,
        text=narration_text,
        duration_s=target_for_estimate,
        unit=timing_unit,
    )
    estimated = any(w.estimated for w in audio_words)

    # 2. Re-time onto the real video duration (single clip or chained segments).
    retimed, duration = _retime(audio_words, duration_s=duration_s, segments=segments)
    video_end_s = round(video_start_s + duration, 3)

    # 3. Align to the page's word boxes for word_index + bbox (reuse §9.4 helpers).
    page = _span_page(source_span)
    word_range = _span_range(source_span)
    source = source_words_in_span(page_word_boxes, word_range)
    alignment = align_words([w.text for w in retimed], [s.text for s in source])
    span_start = word_range[0] if word_range else 0

    words: list[SyncWord] = []
    for i, w in enumerate(retimed):
        src_idx = alignment.source_indices[i] if i < len(alignment.source_indices) else -1
        if 0 <= src_idx < len(source):
            src = source[src_idx]
            word_index = src.word_index
            painted = src.text or w.text
            bbox = src.bbox
        else:
            word_index = span_start + i
            painted = w.text
            bbox = None
        words.append(
            SyncWord(
                word_index=word_index,
                text=painted,
                t_start=round(video_start_s + w.t_start, 3),
                t_end=round(min(video_end_s, video_start_s + w.t_end), 3),
                bbox=bbox,
                estimated=w.estimated,
            )
        )

    # 4. Per-sentence anchors + page-turn marker → the §9.4 shape.
    sentences = _build_sentences(words, narration_text)
    return SyncMap(
        shot_id=shot_id,
        video_start_s=round(video_start_s, 3),
        video_end_s=video_end_s,
        page=page,
        page_turn_at_s=page_turn_at(video_start_s, video_end_s, lead_s=page_turn_lead_s),
        words=words,
        sentences=sentences,
        estimated=estimated,
    )


__all__ = ["build_sync_map"]
