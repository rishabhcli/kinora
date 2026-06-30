"""Deterministic passage → beats segmentation (§4.2 "book → scene → shot → beat").

When a passage arrives as raw text (no pre-segmented beats), the planner must
first cut it into beats — the smallest planning atoms (a sentence-or-two of
narrative intent). This is a pure function: sentences are grouped greedily up to a
narration-word target, each beat carries its absolute global word range (the
scroll-sync key, offset by the passage's ``word_offset``), and each beat's pacing
tempo is classified by the existing comprehension heuristic so downstream coverage
and budget honour the prose's rhythm.

No I/O, no network — the LLM-backed reasoning provider may *replace* this with a
smarter split, but the engine always has this deterministic fallback.
"""

from __future__ import annotations

from app.agents.comprehension.pacing import classify_tempo
from app.agents.comprehension.text_utils import split_sentences, words

from .models import Passage, PassageBeat

#: A beat groups sentences up to roughly this many narration words (~a sentence
#: or two). Kept independent of the per-shot word target so a beat can still
#: split into multiple shots later if its tempo earns dense coverage.
DEFAULT_WORDS_PER_BEAT = 45


def segment_passage(
    passage: Passage, *, words_per_beat: int = DEFAULT_WORDS_PER_BEAT
) -> list[PassageBeat]:
    """Segment a passage's ``text`` into ordered :class:`PassageBeat`s.

    Greedy sentence grouping: sentences are accumulated into a beat until adding
    the next would exceed ``words_per_beat`` (a single over-long sentence still
    forms its own beat). Word ranges are absolute — anchored at the passage's
    ``word_offset`` and advanced by each beat's word count — so they slot into the
    global source-span index. Each beat inherits the passage's entities/page and
    is tempo-classified from its own text.

    Returns ``[]`` for empty text. Idempotent and deterministic.
    """
    target = max(8, words_per_beat)
    sentences = split_sentences(passage.text)
    if not sentences:
        return []

    # Group sentences greedily by accumulated word count.
    groups: list[list[str]] = []
    current: list[str] = []
    current_words = 0
    for sent in sentences:
        n = len(words(sent.text))
        if current and current_words + n > target:
            groups.append(current)
            current = []
            current_words = 0
        current.append(sent.text)
        current_words += n
    if current:
        groups.append(current)

    beats: list[PassageBeat] = []
    cursor = passage.word_offset
    base = passage.passage_id
    for idx, group in enumerate(groups):
        text = " ".join(group).strip()
        n_words = len(words(text))
        lo = cursor
        hi = cursor + n_words
        cursor = hi
        beats.append(
            PassageBeat(
                beat_id=f"{base}_beat_{idx:03d}",
                text=text,
                word_range=(lo, hi),
                page=passage.page,
                entities=list(passage.context.entities),
                tempo=classify_tempo(text).tempo,
                mood=None,
            )
        )
    return beats


__all__ = ["DEFAULT_WORDS_PER_BEAT", "segment_passage"]
