"""Canonical models for the backend-agnostic sync-map layer (§9.4).

The §9.4 sync map binds *video-time ↔ page ↔ word*: it powers the karaoke
highlight, the page-turn events, and the scroll⟷video seek. The original builder
(:mod:`app.render.sync_map`) was written against **one** narration shape — the
hosted CosyVoice/Qwen ``word_timestamps`` — and **one** clip duration. This layer
generalizes that: it accepts *any* provider timing shape, estimates timings when a
backend returns none, and re-times audio onto the *actual* (possibly clamped or
chained-multi-segment) video duration of whatever model rendered the clip.

These models are the contract between the stages:

* :class:`WordTiming` — the one canonical per-word unit every ingest path produces;
* :class:`RawCue` — a subtitle-style cue (SRT/VTT line) before it is split to words;
* :class:`ClipSegment` — one provider clip in a chained multi-segment shot;
* :class:`SyncWord` / :class:`SyncSentence` / :class:`SyncMap` — the **output**,
  shaped identically to what the reading room already consumes
  (:class:`app.render.sync_map.SyncWord` / ``SyncSegment``) plus per-sentence anchors.

Everything is pure pydantic-v2 / dataclasses — no DB, no network, no ffmpeg — so the
whole pipeline is exhaustively unit-testable and deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# How a provider expressed (or failed to express) its timings
# --------------------------------------------------------------------------- #


class TimingShape(StrEnum):
    """The wildly different shapes a backend returns word/character timings in.

    The ingest layer normalizes any of these into a :class:`WordTiming` timeline.
    """

    #: One stamped entry per spoken word (CosyVoice/Qwen ASR, ElevenLabs words).
    PER_WORD = "per_word"
    #: One stamped entry per character/grapheme (ElevenLabs ``alignment``,
    #: Whisper char-mode); coalesced into words at whitespace.
    PER_CHAR = "per_char"
    #: One stamped entry per sub-word token/piece (Whisper/LLM token streams);
    #: re-joined into words (leading-space or ``##`` word-piece conventions).
    PER_TOKEN = "per_token"
    #: Subtitle cues (SRT / WebVTT) — a phrase per cue; words are distributed
    #: inside each cue's ``[start, end]`` by length weight.
    CUE = "cue"
    #: The backend returned no timings at all → the forced-alignment estimator runs.
    NONE = "none"


# --------------------------------------------------------------------------- #
# Canonical per-word timing (the single internal unit)
# --------------------------------------------------------------------------- #


class WordTiming(BaseModel):
    """One narrated word on the **audio** clock (seconds from clip start).

    This is the normalized unit every ingest path produces, regardless of the
    provider shape it came from. ``t_start``/``t_end`` are on the narration's own
    timeline; the retime stage maps them onto the rendered video's timeline.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    t_start: float
    t_end: float
    #: ``True`` when this word's timing was *estimated* (forced-alignment) rather
    #: than reported by the provider — surfaced so QA can flag low-confidence sync.
    estimated: bool = False

    @property
    def duration(self) -> float:
        return max(0.0, self.t_end - self.t_start)


class RawCue(BaseModel):
    """A subtitle-style cue: a phrase with a single ``[start, end]`` window.

    The text of one SRT/VTT entry; :func:`app.video.sync.ingest.ingest_timings`
    distributes its words inside the window by length weight.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    t_start: float
    t_end: float


# --------------------------------------------------------------------------- #
# Multi-segment shots (one logical shot rendered as N chained provider clips)
# --------------------------------------------------------------------------- #


class ClipSegment(BaseModel):
    """One rendered provider clip in a chained multi-segment shot.

    A long shot may be produced as several shorter provider clips concatenated
    back-to-back (the §9.6 stitch, or a model whose max clip length is < the shot).
    ``duration_s`` is the **actual** rendered length of *this* clip (probed from the
    file), which is what the audio timings must be re-timed onto — provider clips are
    routinely clamped/retimed away from the requested length.
    """

    model_config = ConfigDict(extra="forbid")

    #: Stable id for this physical clip (used only for traceability).
    clip_id: str
    #: The actual probed duration of this clip in seconds.
    duration_s: float = Field(gt=0.0)


# --------------------------------------------------------------------------- #
# Output: the reading-room sync-map shape (§9.4)
# --------------------------------------------------------------------------- #


class SyncWord(BaseModel):
    """One karaoke word on the **video** playhead — the reading-room shape (§9.4).

    Field-compatible with :class:`app.render.sync_map.SyncWord` so the existing
    highlight layer consumes it unchanged, plus an ``estimated`` provenance flag.
    """

    model_config = ConfigDict(extra="forbid")

    word_index: int
    text: str
    t_start: float
    t_end: float
    #: Normalized ``[x, y, w, h]`` page-box; ``None`` when the page has no box.
    bbox: list[float] | None = None
    estimated: bool = False


class SyncSentence(BaseModel):
    """A per-sentence anchor: a sentence's span on the playhead (§9.4 anchors).

    Lets the reading room scroll/auto-advance at sentence granularity and gives the
    page-turn logic a sensible unit. ``word_start``/``word_end`` index into the
    segment's ``words`` list (inclusive).
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    t_start: float
    t_end: float
    word_start: int
    word_end: int


class SyncMap(BaseModel):
    """The §9.4 per-shot sync map: karaoke spans + page-turn + sentence anchors.

    Shaped identically to :class:`app.render.sync_map.SyncSegment` (so it merges
    into a scene map and the reading room paints it directly) with two additions:
    ``sentences`` (per-sentence anchors) and ``estimated`` (whether any word timing
    was estimated rather than provider-reported).
    """

    model_config = ConfigDict(extra="forbid")

    shot_id: str
    video_start_s: float
    video_end_s: float
    page: int
    page_turn_at_s: float
    words: list[SyncWord] = Field(default_factory=list)
    sentences: list[SyncSentence] = Field(default_factory=list)
    #: ``True`` when the source timings were estimated (no provider timings).
    estimated: bool = False

    @property
    def duration_s(self) -> float:
        return max(0.0, self.video_end_s - self.video_start_s)


# --------------------------------------------------------------------------- #
# Loose input adapters (accept TtsWord / mappings / objects interchangeably)
# --------------------------------------------------------------------------- #

#: Anything we can read a ``(text, t_start, t_end)`` triple out of: a pydantic
#: ``TtsWord``/``WordTiming``, a frozen dataclass, or a plain mapping.
TimingLike = WordTiming | Mapping[str, Any] | Any


def coerce_word(word: TimingLike) -> WordTiming:
    """Normalize any word-timing-like value into a :class:`WordTiming`.

    Accepts a mapping (``{"text", "t_start", "t_end"[, "estimated"]}``) or any
    object exposing ``.text``/``.t_start``/``.t_end`` attributes (``TtsWord``,
    ``TimedWord``, ``WordTiming``). Missing fields default to empty / ``0.0``.
    """
    if isinstance(word, WordTiming):
        return word
    if isinstance(word, Mapping):
        return WordTiming(
            text=str(word.get("text", "")),
            t_start=float(word.get("t_start", 0.0)),
            t_end=float(word.get("t_end", 0.0)),
            estimated=bool(word.get("estimated", False)),
        )
    return WordTiming(
        text=str(getattr(word, "text", "")),
        t_start=float(getattr(word, "t_start", 0.0)),
        t_end=float(getattr(word, "t_end", 0.0)),
        estimated=bool(getattr(word, "estimated", False)),
    )


def coerce_words(words: Sequence[TimingLike]) -> list[WordTiming]:
    """Vectorized :func:`coerce_word`."""
    return [coerce_word(w) for w in words]


__all__ = [
    "ClipSegment",
    "RawCue",
    "SyncMap",
    "SyncSentence",
    "SyncWord",
    "TimingLike",
    "TimingShape",
    "WordTiming",
    "coerce_word",
    "coerce_words",
]
