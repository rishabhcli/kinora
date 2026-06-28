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


class SyncPhoneme(BaseModel):
    """One sub-word timing chunk inside a narrated word (§9.4 richer sync).

    Phonemes let the karaoke highlight animate *within* a long word and give future
    viseme / mouth-shape work (Phase 10) a per-chunk anchor. Timings are absolute on
    the same playhead as the parent :class:`SyncWord` (so they shift in lockstep when
    a segment is merged onto a scene timeline). Produced by :func:`split_phonemes`,
    which distributes the word's real ``[t_start, t_end]`` across grapheme chunks —
    never inventing duration, exactly like the proportional word aligner.
    """

    model_config = ConfigDict(extra="forbid")

    #: The grapheme chunk (a rough phoneme/syllable unit), e.g. ``"st"`` of "stood".
    text: str
    t_start: float
    t_end: float


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
    #: Optional sub-word phoneme timings (empty by default → backwards compatible;
    #: a client that doesn't read them is unaffected). Filled when the segment is
    #: built with ``phoneme_timing=True``.
    phonemes: list[SyncPhoneme] = Field(default_factory=list)


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
    # Read-only members (properties) so the protocol accepts BOTH a mutable pydantic
    # ``TtsWord`` and a frozen dataclass like :class:`TimedWord` — mutable protocol
    # attributes are invariant and would reject a read-only (frozen) field.
    @property
    def text(self) -> str: ...
    @property
    def t_start(self) -> float: ...
    @property
    def t_end(self) -> float: ...


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


_VOWELS = frozenset("aeiouy")


def grapheme_chunks(word: str) -> list[str]:
    """Split a word into rough phoneme/syllable chunks (deterministic, pure).

    Not a true phonemiser (that needs a pronunciation dict / model); a *grapheme*
    heuristic good enough to drive sub-word highlight + viseme anchors: each chunk is
    a run of leading consonants followed by a run of vowels (an onset+nucleus), with a
    trailing consonant run attached to the last chunk. Punctuation is stripped first.
    Always returns at least one chunk for a non-empty word; an empty/punctuation-only
    word yields ``[]``.

    Examples:
        ``"stood"`` → ``["stoo", "d"]``  (onset ``st`` + nucleus ``oo``, coda ``d``)
        ``"meadow"`` → ``["mea", "dow"]``
        ``"a"`` → ``["a"]``
    """
    core = _TOKEN_STRIP.sub("", word.lower())
    if not core:
        return []
    chunks: list[str] = []
    i = 0
    n = len(core)
    while i < n:
        start = i
        # Onset: leading consonants.
        while i < n and core[i] not in _VOWELS:
            i += 1
        # Nucleus: the vowel run.
        while i < n and core[i] in _VOWELS:
            i += 1
        # If we consumed only consonants (no vowel followed, e.g. trailing "d"),
        # attach them to the previous chunk rather than emitting a vowel-less chunk.
        chunk = core[start:i]
        if not any(c in _VOWELS for c in chunk) and chunks:
            chunks[-1] += chunk
        else:
            chunks.append(chunk)
    return chunks or [core]


def split_phonemes(text: str, t_start: float, t_end: float) -> list[SyncPhoneme]:
    """Distribute a word's ``[t_start, t_end]`` across its grapheme chunks (pure).

    Anchored to the real word timing — the chunks' spans always sum back to the
    word's duration, weighted by chunk length (longer chunks get proportionally more
    time), so a long word's highlight sweeps smoothly. Returns ``[]`` for a
    zero/negative duration or a chunk-less (punctuation-only) word.
    """
    if t_end <= t_start:
        return []
    chunks = grapheme_chunks(text)
    if not chunks:
        return []
    weights = [len(c) for c in chunks]
    total = sum(weights) or len(chunks)
    span = t_end - t_start
    phonemes: list[SyncPhoneme] = []
    cursor = t_start
    for idx, (chunk, weight) in enumerate(zip(chunks, weights, strict=True)):
        portion = span * (weight / total)
        start = cursor
        # Snap the final chunk's end exactly to t_end (no float drift past the word).
        end = t_end if idx == len(chunks) - 1 else min(t_end, start + portion)
        phonemes.append(
            SyncPhoneme(text=chunk, t_start=round(start, 3), t_end=round(end, 3))
        )
        cursor = end
    return phonemes


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
# Narration ↔ clip retiming
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TimedWord:
    """A minimal retimed word (the shape ``build_sync_segment`` accepts as input)."""

    text: str
    t_start: float
    t_end: float


def rescale_word_timings(
    word_timestamps: Sequence[NarratedWord],
    *,
    target_duration_s: float,
) -> list[TimedWord]:
    """Linearly rescale narrated word timings to fit a target clip duration (pure).

    The TTS narration rarely lands at exactly the rendered clip's length; if the
    karaoke timings ran on the *narration* clock they would drift against the video.
    This rescales every word time by ``target / narration_span`` so the last word
    ends exactly at ``target_duration_s`` and the highlight stays locked to the
    clip (§9.4). A zero/empty narration span returns the words unchanged (nothing to
    anchor to). The relative spacing of words is preserved — only the global tempo
    is stretched/compressed.
    """
    words = [_narrated_tuple(w) for w in word_timestamps]
    if not words or target_duration_s <= 0:
        return [TimedWord(t, a, b) for t, a, b in words]
    narration_span = max(t_end for _, _, t_end in words)
    if narration_span <= 0:
        return [TimedWord(t, a, b) for t, a, b in words]
    factor = target_duration_s / narration_span
    return [
        TimedWord(
            text=text,
            t_start=round(t0 * factor, 3),
            t_end=round(min(target_duration_s, t1 * factor), 3),
        )
        for text, t0, t1 in words
    ]


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
    phoneme_timing: bool = False,
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
        phoneme_timing: when ``True``, each word is split into grapheme/phoneme
            sub-chunks (:func:`split_phonemes`) so the karaoke highlight can animate
            within a long word and viseme work has anchors. Off by default (the
            field stays empty → backwards compatible).
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
        word_start = round(min(video_end_s, video_start_s + t0), 3)
        word_end = round(min(video_end_s, video_start_s + t1), 3)
        phonemes = (
            split_phonemes(painted, word_start, word_end) if phoneme_timing else []
        )
        words.append(
            SyncWord(
                word_index=word_index,
                text=painted,
                t_start=word_start,
                t_end=word_end,
                bbox=bbox,
                phonemes=phonemes,
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
    "SyncPhoneme",
    "SyncSegment",
    "SyncWord",
    "TimedWord",
    "WordAlignment",
    "align_words",
    "build_sync_segment",
    "grapheme_chunks",
    "normalize_token",
    "page_turn_at",
    "rescale_word_timings",
    "source_words_in_span",
    "split_phonemes",
]
