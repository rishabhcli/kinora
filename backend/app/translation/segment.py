"""Segment-aware splitting of source content into translation units.

A whole page (or a long narration script) is too coarse to translate as one
blob: a single dropped placeholder anywhere fails the whole page, the cache
granularity is too large to reuse, and an LLM is more accurate on bounded units.
So content is split into *segments* — paragraph or sentence units — each of
which becomes a cache key, a quality score, and (if low-confidence) a review row.

The splitter is language-aware: it honours the per-language sentence terminators
from the registry (Arabic ``؟``, CJK ``。``) and never splits *inside* a
protected markup/placeholder run (so ``Dr.`` or ``{name. tag}`` is not mistaken
for a sentence boundary). It is reversible: :func:`join_segments` reconstructs
the original text from the segments + their separators, so a translated set can
be stitched back into a page in order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .languages import get_language
from .markup import mask
from .types import ContentKind, Segment

# A blank line (optionally with trailing whitespace) separates paragraphs.
_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n")

# Abbreviations that end in a period but do not end a sentence (English-centric;
# extended per-language as needed). Kept small + high-frequency.
_ABBREVIATIONS = frozenset(
    {"mr", "mrs", "ms", "dr", "prof", "st", "sr", "jr", "vs", "etc", "e.g", "i.e", "no"}
)


@dataclass(frozen=True, slots=True)
class SegmentSpan:
    """A segment + how to put it back (its trailing separator)."""

    segment: Segment
    separator: str  # the whitespace/break that followed this unit in the source


def split_sentences(text: str, lang: str = "en") -> list[str]:
    """Split a paragraph into sentences, honouring language + markup.

    Sentence boundaries are the language's terminators followed by whitespace and
    a likely sentence start. Boundaries inside a protected run (see
    :mod:`.markup`) are suppressed, and an abbreviation immediately before the
    terminator does not break.
    """
    language = get_language(lang)
    terminators = set(language.sentence_terminators)

    # Compute the masked text so we know which spans are *inside* a placeholder
    # (sentinels contain no terminators, so a boundary can only fall in real
    # text). We split on the ORIGINAL text but use mask only to confirm the
    # terminator isn't part of a protected run by checking position parity.
    protected_ranges = _protected_ranges(text)

    sentences: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in terminators and not _in_ranges(i, protected_ranges):
            # Consume a run of terminators / closing quotes.
            j = i + 1
            while j < n and (text[j] in terminators or text[j] in "\"')]”’»"):
                j += 1
            # Require whitespace (or end) after, else it's e.g. a decimal "3.5".
            if j >= n or text[j].isspace():
                candidate = text[start:j].strip()
                if candidate and not _ends_with_abbreviation(text[start:i]):
                    sentences.append(candidate)
                    # Skip following whitespace.
                    while j < n and text[j].isspace():
                        j += 1
                    start = j
                    i = j
                    continue
            i = j
            continue
        i += 1
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _ends_with_abbreviation(text: str) -> bool:
    token = re.split(r"[\s]", text.strip())[-1] if text.strip() else ""
    return token.lower().rstrip(".") in _ABBREVIATIONS


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    """Char ranges occupied by protected markup runs (to suppress boundaries)."""
    masked = mask(text)
    if not masked.tokens:
        return []
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for tok in masked.tokens:
        idx = text.find(tok, cursor)
        if idx == -1:
            continue
        ranges.append((idx, idx + len(tok)))
        cursor = idx + len(tok)
    return ranges


def _in_ranges(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(lo <= pos < hi for lo, hi in ranges)


def split_paragraphs(text: str) -> list[str]:
    """Split on blank lines into paragraphs (whitespace-trimmed, non-empty)."""
    return [p.strip() for p in _PARAGRAPH_RE.split(text) if p.strip()]


def segment_text(
    text: str,
    *,
    base_id: str,
    kind: ContentKind = ContentKind.PAGE_TEXT,
    lang: str = "en",
    granularity: str = "sentence",
    metadata: dict[str, object] | None = None,
) -> list[Segment]:
    """Split ``text`` into :class:`Segment` units with stable, ordered ids.

    Args:
        base_id: Id prefix; each segment gets ``{base_id}.{n}``.
        granularity: ``"sentence"`` (default), ``"paragraph"``, or ``"whole"``.
        kind: Content kind stamped on every produced segment.
        lang: Language for sentence-boundary detection.
    """
    meta = dict(metadata or {})
    if granularity == "whole":
        units = [text.strip()] if text.strip() else []
    elif granularity == "paragraph":
        units = split_paragraphs(text)
    else:
        units = []
        for para in split_paragraphs(text):
            units.extend(split_sentences(para, lang))
    segments: list[Segment] = []
    for n, unit in enumerate(units):
        segments.append(
            Segment(
                id=f"{base_id}.{n}",
                text=unit,
                kind=kind,
                metadata={**meta, "index": n},
            )
        )
    return segments


def join_segments(texts: list[str], *, separator: str = " ") -> str:
    """Reassemble translated segment texts into one string."""
    return separator.join(t for t in texts if t)


__all__ = [
    "SegmentSpan",
    "join_segments",
    "segment_text",
    "split_paragraphs",
    "split_sentences",
]
