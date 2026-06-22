"""Build the per-shot sync map from narration timings + page word boxes (§9.4).

The sync map is the artifact that binds *video-time ↔ page ↔ word* and powers
both the read-along karaoke highlight and the scroll⟷video seek (§5.2, §5.3).
One :class:`SyncSegment` is produced per shot:

* ``video_start_s`` / ``video_end_s`` — the shot's window on the playhead;
* ``page`` + ``page_turn_at_s`` — when the SyncEngine flips the PDF (slightly
  *before* the shot ends, so the next page is settled before the next shot);
* ``words`` — every narrated word stamped with its ``t_start``/``t_end`` (from
  the TTS word timestamps) **and** a ``word_index`` + normalized ``bbox`` taken
  from the page's word boxes, so the highlight layer can paint it and the
  ``word_index`` ties back into the source-span index.

Everything here is pure and dependency-light (no DB, no network, no ffmpeg) so
the alignment is exhaustively unit-testable. The narrated words are aligned to
the source words of the shot's span by position when the counts match
("exact"), and by proportional distribution across the span when they differ —
the §9.4 fallback that still yields a sensible ``word_index``/``bbox`` for every
narrated word.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

_TOKEN_STRIP = re.compile(r"[^0-9a-z]+")
#: Default lead before the shot's end at which the page turns (§9.4 example: 4.8 of 5.0).
_DEFAULT_PAGE_TURN_LEAD_S = 0.2


# --------------------------------------------------------------------------- #
# Output models (the §9.4 sync-map shape)
# --------------------------------------------------------------------------- #


class SyncWord(BaseModel):
    """One narrated word: its timing plus the page geometry to highlight it."""

    model_config = ConfigDict(extra="forbid")

    word_index: int
    text: str
    t_start: float
    t_end: float
    #: Normalized ``[x, y, w, h]`` page-box for the highlight layer (``None`` when
    #: the page has no box for this word).
    bbox: list[float] | None = None


class SyncSegment(BaseModel):
    """The §9.4 per-shot sync segment (merged into a scene sync map at stitch)."""

    model_config = ConfigDict(extra="forbid")

    shot_id: str
    video_start_s: float
    video_end_s: float
    page: int
    page_turn_at_s: float
    words: list[SyncWord] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Narrated-word input adapter (accept TtsWord or a plain mapping)
# --------------------------------------------------------------------------- #


@runtime_checkable
class _HasTiming(Protocol):
    text: str
    t_start: float
    t_end: float


NarratedWord = _HasTiming | Mapping[str, Any]


def _narrated_tuple(word: NarratedWord) -> tuple[str, float, float]:
    """Normalize a narrated word (``TtsWord`` or mapping) to ``(text, t0, t1)``."""
    if isinstance(word, Mapping):
        return (
            str(word.get("text", "")),
            float(word.get("t_start", 0.0)),
            float(word.get("t_end", 0.0)),
        )
    return (str(word.text), float(word.t_start), float(word.t_end))


# --------------------------------------------------------------------------- #
# Source-word extraction from the page's word boxes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SourceWord:
    """One page word inside a shot's span: its global index, text, and box."""

    word_index: int
    text: str
    bbox: list[float] | None


def _parse_bbox(raw: Any) -> list[float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        return [float(v) for v in raw]
    except (TypeError, ValueError):
        return None


def source_words_in_span(
    page_word_boxes: Sequence[Mapping[str, Any]] | None,
    word_range: tuple[int, int] | None,
) -> list[SourceWord]:
    """Return the page's words inside ``word_range`` (inclusive), in reading order.

    When ``word_range`` is ``None`` or degenerate (``[0, 0]``), every word box on
    the page is returned — the best available anchor when the span is unknown.
    """
    if not page_word_boxes:
        return []
    start, end = word_range if word_range else (None, None)
    use_range = word_range is not None and not (start == 0 and end == 0)
    out: list[SourceWord] = []
    for box in page_word_boxes:
        idx_raw = box.get("word_index")
        if idx_raw is None:
            continue
        idx = int(idx_raw)
        if use_range and not (start <= idx <= end):  # type: ignore[operator]
            continue
        out.append(
            SourceWord(
                word_index=idx,
                text=str(box.get("text", "")),
                bbox=_parse_bbox(box.get("bbox")),
            )
        )
    out.sort(key=lambda w: w.word_index)
    return out


# --------------------------------------------------------------------------- #
# Alignment (the §9.4 narrated → source mapping)
# --------------------------------------------------------------------------- #


def normalize_token(text: str) -> str:
    """Lowercase + strip surrounding punctuation for tolerant word matching."""
    return _TOKEN_STRIP.sub("", text.lower())


@dataclass(frozen=True, slots=True)
class WordAlignment:
    """The chosen narrated→source mapping and how it was derived.

    ``source_indices[i]`` is the index into the source-word list that narrated
    word ``i`` was aligned to (or ``-1`` when there are no source words).
    """

    method: Literal["exact", "proportional", "fallback"]
    source_indices: list[int]


def align_words(narrated_texts: Sequence[str], source_texts: Sequence[str]) -> WordAlignment:
    """Align narrated words to source words (§9.4).

    * equal counts            -> 1:1 positional ("exact"; tolerant of punctuation);
    * differing counts        -> proportional distribution across the span;
    * no source words at all   -> "fallback" (the caller indexes off the span start).
    """
    n_nar = len(narrated_texts)
    n_src = len(source_texts)
    if n_src == 0:
        return WordAlignment(method="fallback", source_indices=[-1] * n_nar)
    if n_nar == n_src:
        return WordAlignment(method="exact", source_indices=list(range(n_nar)))
    # Proportional: spread the narrated words evenly across the source words so
    # every narrated word still lands on a real page word_index + bbox.
    indices = [min(n_src - 1, (i * n_src) // n_nar) for i in range(n_nar)] if n_nar else []
    return WordAlignment(method="proportional", source_indices=indices)


# --------------------------------------------------------------------------- #
# page_turn_at_s
# --------------------------------------------------------------------------- #


def page_turn_at(
    video_start_s: float, video_end_s: float, *, lead_s: float = _DEFAULT_PAGE_TURN_LEAD_S
) -> float:
    """When to flip the page: ``lead_s`` before the shot ends, clamped sanely.

    Guaranteed ``video_start_s <= page_turn_at_s < video_end_s`` for any positive
    duration so the next page is settled before the next shot starts (§9.4).
    """
    duration = max(0.0, video_end_s - video_start_s)
    if duration <= 0.0:
        return round(video_end_s, 3)
    # At least ``lead_s`` (≈0.2s, the §9.4 example) but never more than 90% of the
    # shot, and at least a hair so the strict ``< video_end_s`` invariant holds.
    lead = min(max(lead_s, duration * 0.04), duration * 0.9)
    turn = video_end_s - lead
    return round(max(video_start_s, turn), 3)


# --------------------------------------------------------------------------- #
# The builder
# --------------------------------------------------------------------------- #


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


def _resolve_duration(
    duration_s: float | None, narrated: Sequence[tuple[str, float, float]]
) -> float:
    if duration_s is not None:
        return max(0.0, duration_s)
    if narrated:
        return max(0.0, max(t_end for _, _, t_end in narrated))
    return 0.0


def build_sync_segment(
    *,
    shot_id: str,
    word_timestamps: Sequence[NarratedWord],
    source_span: Mapping[str, Any] | None,
    page_word_boxes: Sequence[Mapping[str, Any]] | None,
    video_start_s: float = 0.0,
    duration_s: float | None = None,
    page_turn_lead_s: float = _DEFAULT_PAGE_TURN_LEAD_S,
) -> SyncSegment:
    """Assemble the §9.4 :class:`SyncSegment` for one shot (pure).

    Args:
        shot_id: the shot this segment belongs to.
        word_timestamps: the narration's per-word timings (TTS / forced-align).
        source_span: ``{"page", "word_range": [start, end]}`` for the shot.
        page_word_boxes: the page's ``[{word_index, text, bbox}]`` (PyMuPDF).
        video_start_s: the shot's start on the playhead (0 for a standalone shot;
            the scene offset when building inside a stitched timeline).
        duration_s: the clip duration; when ``None`` it is taken from the last
            narrated word's ``t_end``.
        page_turn_lead_s: how far before the end to flip the page.
    """
    narrated = [_narrated_tuple(w) for w in word_timestamps]
    duration = _resolve_duration(duration_s, narrated)
    video_end_s = round(video_start_s + duration, 3)
    page = _span_page(source_span)
    word_range = _span_range(source_span)

    source = source_words_in_span(page_word_boxes, word_range)
    alignment = align_words([t[0] for t in narrated], [s.text for s in source])

    span_start = word_range[0] if word_range else 0
    words: list[SyncWord] = []
    for i, (text, t0, t1) in enumerate(narrated):
        src_idx = alignment.source_indices[i] if i < len(alignment.source_indices) else -1
        if 0 <= src_idx < len(source):
            src = source[src_idx]
            word_index = src.word_index
            painted = src.text or text
            bbox = src.bbox
        else:
            # No page geometry: index sequentially off the span start, paint the
            # spoken word, and leave bbox empty (the highlight layer no-ops).
            word_index = span_start + i
            painted = text
            bbox = None
        words.append(
            SyncWord(
                word_index=word_index,
                text=painted,
                t_start=round(min(video_end_s, video_start_s + t0), 3),
                t_end=round(min(video_end_s, video_start_s + t1), 3),
                bbox=bbox,
            )
        )

    return SyncSegment(
        shot_id=shot_id,
        video_start_s=round(video_start_s, 3),
        video_end_s=video_end_s,
        page=page,
        page_turn_at_s=page_turn_at(video_start_s, video_end_s, lead_s=page_turn_lead_s),
        words=words,
    )


__all__ = [
    "NarratedWord",
    "SourceWord",
    "SyncSegment",
    "SyncWord",
    "WordAlignment",
    "align_words",
    "build_sync_segment",
    "normalize_token",
    "page_turn_at",
    "source_words_in_span",
]
