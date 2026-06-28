"""Shared deterministic text primitives for the comprehension engine (§9.1, §10).

Pure, network-free building blocks the literary-comprehension passes all reuse:
sentence segmentation that respects quotation marks and common abbreviations,
quote-span extraction across the curly/straight/guillemet families, and a small
tokenizer. Keeping these here means every analysis pass (dialogue, POV,
discourse, devices, pacing, timeline) splits text the *same* way, so a beat's
sentence indices line up across passes.

None of this calls a model: the Adapter's LLM pass produces the beats, and these
deterministic functions are what make the per-beat literary analysis
unit-testable without a network (the §10 testability discipline).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Sentence-final punctuation that is *not* an abbreviation period. We split on
# these, then re-attach trailing closing quotes/brackets to the sentence.
_SENTENCE_END = re.compile(r"[.!?]+[\"'”’»\)\]]*\s+")

#: Abbreviations whose trailing period must NOT end a sentence.
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "st", "sr", "jr", "prof", "rev", "hon", "gen",
    "col", "capt", "sgt", "lt", "gov", "pres", "vs", "etc", "e.g", "i.e",
    "no", "vol", "fig", "al", "inc", "ltd", "co",
})

#: The opening→closing quote families we recognise (straight, curly, guillemet).
_QUOTE_PAIRS = (
    ('"', '"'),
    ("“", "”"),  # “ ”
    ("‘", "’"),  # ‘ ’  (also apostrophe — handled by length filter)
    ("«", "»"),  # « »
)
_OPEN_QUOTES = "".join(p[0] for p in _QUOTE_PAIRS)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_TITLECASE_RE = re.compile(r"\b([A-Z][a-z]+)\b")


@dataclass(frozen=True)
class Sentence:
    """One sentence within a beat: its text and its [start, end) char offsets."""

    index: int
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class QuoteSpan:
    """A run of quoted speech: the inner text and its [start, end) char offsets."""

    text: str
    start: int
    end: int


def split_sentences(text: str) -> list[Sentence]:
    """Split ``text`` into sentences, respecting quotes and abbreviations.

    Deterministic and offset-preserving: each :class:`Sentence` carries its exact
    character span so other passes can map a sentence back to its position in the
    beat. Abbreviation periods (``Mr.``) do not end a sentence.
    """
    if not text:
        return []
    sentences: list[Sentence] = []
    start = 0
    idx = 0
    pos = 0
    n = len(text)
    while pos < n:
        match = _SENTENCE_END.search(text, pos)
        if match is None:
            break
        # Reject splits that fall right after a known abbreviation.
        preceding = text[start : match.start()].rstrip()
        last_word = preceding.split()[-1].lower().rstrip(".") if preceding.split() else ""
        if last_word in _ABBREVIATIONS:
            pos = match.end()
            continue
        chunk = text[start : match.end()].strip()
        if chunk:
            real_end = start + len(text[start : match.end()].rstrip())
            sentences.append(Sentence(index=idx, text=chunk, start=start, end=real_end))
            idx += 1
        start = match.end()
        pos = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(
            Sentence(index=idx, text=tail, start=start, end=start + len(text[start:].rstrip()))
        )
    return sentences


def extract_quotes(text: str, *, min_len: int = 2) -> list[QuoteSpan]:
    """Extract quoted-speech spans across the straight/curly/guillemet families.

    A naive but robust scanner: it walks the string, and on an opening quote it
    seeks the matching close. Apostrophe-style single curly quotes are filtered
    by ``min_len`` so contractions ("don't") are not mistaken for speech. Returns
    spans in reading order; nested/unbalanced quotes degrade gracefully (an
    unterminated quote runs to end-of-text).
    """
    spans: list[QuoteSpan] = []
    closers = dict(_QUOTE_PAIRS)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _OPEN_QUOTES:
            close = closers[ch]
            # For straight double quote, the closer == opener; find the next one.
            j = text.find(close, i + 1)
            if j == -1:
                j = n
            inner = text[i + 1 : j].strip()
            if len(inner) >= min_len and _looks_like_speech(inner):
                spans.append(QuoteSpan(text=inner, start=i, end=min(j + 1, n)))
            i = j + 1
        else:
            i += 1
    return spans


def _looks_like_speech(inner: str) -> bool:
    """Reject single-word curly-apostrophe noise; accept real quoted clauses."""
    # A real line of speech has whitespace or sentence punctuation, or is a
    # capitalised multi-letter token. Bare possessives ("’s") are rejected.
    if any(c.isspace() for c in inner):
        return True
    return len(inner) >= 3 and inner[0].isalpha()


def words(text: str) -> list[str]:
    """Lower-cased alphabetic word tokens (contractions/hyphens kept intact)."""
    return [w.lower() for w in _WORD_RE.findall(text)]


def titlecase_names(text: str) -> list[str]:
    """Candidate proper-name tokens: Title-Case words not at a sentence start.

    A cheap proper-noun heuristic used by speaker attribution and POV detection
    when no canon is supplied. It deliberately keeps duplicates in order so the
    caller can reason about *which* name is nearest a dialogue tag.
    """
    return _TITLECASE_RE.findall(text)


def strip_quotes(text: str) -> str:
    """Remove quoted-speech spans from ``text`` (leaving the narration frame).

    Used by passes that should reason about the *narration* around speech (tags,
    POV cues) without the speech itself polluting the signal.
    """
    spans = extract_quotes(text)
    if not spans:
        return text
    out: list[str] = []
    cursor = 0
    for span in spans:
        out.append(text[cursor : span.start])
        cursor = span.end
    out.append(text[cursor:])
    return " ".join(part.strip() for part in out if part.strip())


__all__ = [
    "QuoteSpan",
    "Sentence",
    "extract_quotes",
    "split_sentences",
    "strip_quotes",
    "titlecase_names",
    "words",
]
