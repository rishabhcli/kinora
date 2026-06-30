"""Pure text utilities for the sync layer: tokenizing, syllables, sentences.

Shared by the forced-alignment :mod:`estimator` and the SRT/VTT cue splitter in
:mod:`ingest`. Deterministic and dependency-light (stdlib ``re`` only) so the whole
sync pipeline stays unit-testable. The syllable heuristic and punctuation-pause
model are intentionally simple — good enough to distribute a clip's duration across
words for a believable karaoke sweep, not a phonemiser.
"""

from __future__ import annotations

import re

#: A "word" for timing purposes is any run of non-whitespace (keeps trailing
#: punctuation attached to the word it follows, matching how TTS reports words).
_WORD_RE = re.compile(r"\S+")
#: Sentence-ending punctuation (incl. CJK full-stops / interrobang families).
_SENTENCE_END = re.compile(r"([.!?。！？…]+[\"'”’)\]]*)\s+")
#: Strip surrounding punctuation for syllable counting / token matching.
_ALNUM = re.compile(r"[^0-9a-z]+")
_VOWEL_GROUP = re.compile(r"[aeiouy]+")

#: Pause weight (in "syllable-equivalents") added *after* a word that ends with the
#: given punctuation, so a comma/period earns a real beat of silence in the sweep.
#: Calibrated against natural read-aloud cadence (a full stop pauses ~3× a comma).
_PUNCT_PAUSE = {
    ",": 1.2,
    ";": 1.5,
    ":": 1.3,
    "—": 1.5,
    "–": 1.3,
    ".": 3.0,
    "!": 3.0,
    "?": 3.0,
    "…": 3.5,
    "。": 3.0,
    "！": 3.0,
    "？": 3.0,
}


def tokenize(text: str) -> list[str]:
    """Split text into whitespace-delimited word tokens (punctuation attached)."""
    return _WORD_RE.findall(text)


def syllable_count(word: str) -> int:
    """Estimate a word's syllable count (deterministic heuristic, ``>= 1``).

    Counts vowel groups, drops a silent trailing ``e``, and never returns 0 for a
    word with any alphanumeric content (so every spoken word earns time). A
    punctuation-only / empty token returns 0.
    """
    core = _ALNUM.sub("", word.lower())
    if not core:
        return 0
    groups = _VOWEL_GROUP.findall(core)
    count = len(groups)
    # Silent trailing 'e' (e.g. "stone" → 1, not 2), but never below 1.
    if core.endswith("e") and count > 1 and not core.endswith(("le", "ie")):
        count -= 1
    return max(1, count)


def trailing_pause_weight(word: str) -> float:
    """Extra timing weight for the punctuation pause *after* a word.

    Returns the largest matching pause among the word's trailing punctuation
    characters (so ``"end."`` and ``"end.""`` both earn the full-stop pause).
    """
    best = 0.0
    for ch in reversed(word):
        if ch.isalnum():
            break
        best = max(best, _PUNCT_PAUSE.get(ch, 0.0))
    return best


def word_weight(word: str, *, gap_after: bool = True) -> float:
    """Relative duration weight for a word: syllables + (optional) trailing pause.

    A floor of ``1.0`` keeps zero-syllable tokens (e.g. ``"…"`` or a stray symbol)
    from collapsing to no time. When ``gap_after`` is ``False`` the punctuation
    pause is omitted (used for the final word, which has no following gap).
    """
    base = float(max(1, syllable_count(word)))
    if gap_after:
        base += trailing_pause_weight(word)
    return base


def split_sentences(text: str) -> list[str]:
    """Split text into sentences on terminal punctuation (keeps the punctuation).

    A best-effort, abbreviation-naive splitter: good enough to anchor per-sentence
    scroll units. Always returns at least one entry for non-empty input; collapses
    internal whitespace runs in each returned sentence to single spaces.
    """
    stripped = text.strip()
    if not stripped:
        return []
    # Insert a sentinel after each sentence-ender + following space, then split.
    marked = _SENTENCE_END.sub(lambda m: m.group(1) + "\x00", stripped)
    parts = [p.strip() for p in marked.split("\x00")]
    return [re.sub(r"\s+", " ", p) for p in parts if p]


__all__ = [
    "split_sentences",
    "syllable_count",
    "tokenize",
    "trailing_pause_weight",
    "word_weight",
]
