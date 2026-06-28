"""Viseme track — mouth-shape anchors from the phoneme sync map (§9.4 richer sync).

A *viseme* is the visual mouth shape for a sound; a stream of timed visemes is the
anchor a talking-head / lip-flap overlay animates against, and it doubles as a
richer accessibility cue (a reader who can't hear still sees the mouth move). The
sync map already carries per-word phoneme chunks (:class:`app.render.sync_map.
SyncPhoneme`); this module maps each grapheme chunk onto a small, standard viseme
set and emits a timed :class:`VisemeFrame` stream.

Pure and deterministic — grapheme chunk in, viseme out, no model and no audio. The
mapping is a coarse grapheme→viseme table (a true phoneme→viseme map needs a
pronunciation dictionary, Phase 10); it is good enough to drive a mouth-flap and to
collapse runs of silence. The viseme set is the compact Oculus/JALI-style group:
neutral rest plus the major articulations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.render.sync_map import SyncSegment, SyncWord


class Viseme(StrEnum):
    """A compact, standard mouth-shape set (Oculus LipSync-style groups)."""

    SIL = "sil"  # rest / silence (closed, neutral)
    PP = "PP"  # bilabial plosive/nasal: p, b, m
    FF = "FF"  # labiodental fricative: f, v
    TH = "TH"  # dental: th
    DD = "DD"  # alveolar: t, d, n, l
    KK = "kk"  # velar: k, g, hard c
    CH = "CH"  # postalveolar: ch, j, sh
    SS = "SS"  # sibilant: s, z
    RR = "RR"  # rhotic: r
    AA = "aa"  # open vowel: a
    E = "E"  # mid front vowel: e
    IH = "ih"  # close front vowel: i, y
    OH = "oh"  # rounded back vowel: o
    OU = "ou"  # close rounded: u, w


#: First-letter (and a couple of digraph) grapheme → viseme. Coarse but stable.
_LEADING_VISEME: dict[str, Viseme] = {
    "p": Viseme.PP, "b": Viseme.PP, "m": Viseme.PP,
    "f": Viseme.FF, "v": Viseme.FF,
    "t": Viseme.DD, "d": Viseme.DD, "n": Viseme.DD, "l": Viseme.DD,
    "k": Viseme.KK, "g": Viseme.KK, "c": Viseme.KK, "q": Viseme.KK,
    "s": Viseme.SS, "z": Viseme.SS,
    "r": Viseme.RR,
    "a": Viseme.AA, "e": Viseme.E, "i": Viseme.IH, "y": Viseme.IH,
    "o": Viseme.OH, "u": Viseme.OU, "w": Viseme.OU,
    "h": Viseme.AA, "j": Viseme.CH, "x": Viseme.KK,
}
#: Two-letter digraphs override the single-letter lookup when a chunk starts with them.
_DIGRAPH_VISEME: dict[str, Viseme] = {
    "th": Viseme.TH,
    "ch": Viseme.CH,
    "sh": Viseme.CH,
    "ph": Viseme.FF,
    "wh": Viseme.OU,
    "qu": Viseme.KK,
}


@dataclass(frozen=True, slots=True)
class VisemeFrame:
    """One mouth shape held over a time span on the narration playhead."""

    viseme: Viseme
    t_start: float
    t_end: float

    @property
    def duration_s(self) -> float:
        return max(0.0, self.t_end - self.t_start)


def viseme_for_chunk(chunk: str) -> Viseme:
    """Map one grapheme chunk to its dominant viseme (pure; rest for empty)."""
    text = chunk.strip().lower()
    if not text:
        return Viseme.SIL
    if len(text) >= 2 and text[:2] in _DIGRAPH_VISEME:
        return _DIGRAPH_VISEME[text[:2]]
    return _LEADING_VISEME.get(text[0], Viseme.SIL)


def _coalesce(frames: list[VisemeFrame]) -> list[VisemeFrame]:
    """Merge adjacent identical visemes (and touching silences) into one span."""
    merged: list[VisemeFrame] = []
    for frame in frames:
        prev = merged[-1] if merged else None
        contiguous = prev is not None and abs(prev.t_end - frame.t_start) < 1e-3
        if prev is not None and contiguous and prev.viseme is frame.viseme:
            merged[-1] = VisemeFrame(prev.viseme, prev.t_start, frame.t_end)
        else:
            merged.append(frame)
    return merged


def word_visemes(word: SyncWord) -> list[VisemeFrame]:
    """The viseme frames for one word from its phoneme chunks (pure).

    Falls back to a single rest-to-rest open shape when the word has no phonemes
    (so a word built without ``phoneme_timing`` still yields a sensible mouth flap):
    a single :data:`Viseme.AA`-ish open over the word's span keyed off its text.
    """
    if not word.phonemes:
        return [VisemeFrame(viseme_for_chunk(word.text), word.t_start, word.t_end)]
    return [
        VisemeFrame(viseme_for_chunk(ph.text), ph.t_start, ph.t_end) for ph in word.phonemes
    ]


def segment_visemes(segment: SyncSegment, *, with_rests: bool = True) -> list[VisemeFrame]:
    """Build the viseme track for a whole sync segment (pure).

    Concatenates each word's visemes in order; when ``with_rests`` is set, a
    :data:`Viseme.SIL` rest fills any gap between words (the breath/pause) and any
    lead-in before the first word, so the track is continuous over the segment. The
    track is coalesced (adjacent identical visemes merged) to a compact stream.
    """
    frames: list[VisemeFrame] = []
    cursor = segment.video_start_s
    for word in segment.words:
        if with_rests and word.t_start - cursor > 1e-3:
            frames.append(VisemeFrame(Viseme.SIL, round(cursor, 3), round(word.t_start, 3)))
        frames.extend(word_visemes(word))
        cursor = word.t_end
    if with_rests and segment.video_end_s - cursor > 1e-3:
        frames.append(VisemeFrame(Viseme.SIL, round(cursor, 3), round(segment.video_end_s, 3)))
    return _coalesce(frames)


__all__ = [
    "Viseme",
    "VisemeFrame",
    "segment_visemes",
    "viseme_for_chunk",
    "word_visemes",
]
