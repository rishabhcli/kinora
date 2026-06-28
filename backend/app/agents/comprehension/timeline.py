"""Non-linear timeline reconstruction — narrative-time vs story-time (§4.2).

A beat's **narrative-time** is the order its words appear on the page (the
source-span index keys on this for scroll-sync — it must never change). Its
**story-time** is *when the event actually happens* in the fiction. They diverge
whenever the prose tells events out of order: a flashback narrates an earlier
moment later; a flash-forward narrates a later moment early.

This module:

1. classifies each beat's :class:`TimePosition` from temporal cues
   ("years before", "she remembered", "would later", present continuity);
2. reconstructs a **story-time ordering** — a stable chronological rank per beat
   that respects relative cues while keeping the present-line in reading order —
   so a consumer can replay events in the order they happened (e.g. a "story
   order" recap) WITHOUT disturbing the scroll-synced narrative order.

Pure and deterministic. Reconstruction is intentionally conservative: when the
cues are ambiguous it falls back to narrative order, never fabricating a
timeline the text doesn't support (§10 in spirit — no invented structure).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from app.agents.contracts import TimePosition

# Past-shift cues (an earlier story moment narrated now) → FLASHBACK.
_BACK_CUES = (
    r"years?\s+(?:before|earlier|ago|prior)",
    r"months?\s+(?:before|earlier|ago)",
    r"days?\s+(?:before|earlier|ago)",
    r"weeks?\s+(?:before|earlier|ago)",
    r"long\s+ago",
    r"once,?\s+(?:when|as|long)",
    r"(?:he|she|they|i)\s+(?:remembered|recalled|recollected)",
    r"had\s+(?:once|long|always|never)",
    r"back\s+(?:then|when)",
    r"in\s+(?:those|the\s+old)\s+days",
    r"as\s+a\s+(?:child|boy|girl|young)",
    r"the\s+(?:summer|winter|year|night)\s+(?:before|when)",
)
# Future-shift cues (a later story moment narrated now) → FLASHFORWARD.
_FORWARD_CUES = (
    r"would\s+(?:later|soon|one\s+day|eventually|never)",
    r"years?\s+(?:later|after|hence|from\s+now)",
    r"months?\s+later",
    r"days?\s+later",
    r"in\s+the\s+(?:years|days|months)\s+(?:to\s+come|ahead)",
    r"someday",
    r"one\s+day\s+(?:she|he|they|i)\s+would",
    r"by\s+the\s+time",
    r"little\s+did\s+(?:she|he|they|i)\s+know",
)
# Habitual/gnomic cues (no fixed point on the line) → TIMELESS.
_TIMELESS_CUES = (
    r"every\s+(?:day|night|morning|year|summer|winter)",
    r"always\s+(?:had|did|would)",
    r"used\s+to",
    r"on\s+(?:sundays|mondays|tuesdays|wednesdays|thursdays|fridays|saturdays)",
    r"each\s+(?:day|night|morning|evening|year)",
)

_BACK_RE = re.compile("|".join(_BACK_CUES), re.IGNORECASE)
_FWD_RE = re.compile("|".join(_FORWARD_CUES), re.IGNORECASE)
_TIMELESS_RE = re.compile("|".join(_TIMELESS_CUES), re.IGNORECASE)

# Cues that the narration has RETURNED to the present line, closing a flashback.
_RESUME_RE = re.compile(
    r"\b(?:now|today|at\s+present|back\s+in\s+the\s+present|"
    r"the\s+present|returned\s+to|in\s+the\s+here\s+and\s+now)\b",
    re.IGNORECASE,
)
# Past-perfect ("had <participle>") keeps an OPEN flashback block open — the
# narration is still recounting the earlier story moment, not the now-line.
_PAST_PERFECT_RE = re.compile(r"\b(?:had|'d)\s+\w+(?:ed|en|ought|ung|ad|one)\b", re.IGNORECASE)


def _continues_flashback(text: str) -> bool:
    """Whether a beat (lacking its own shift cue) stays inside an open flashback.

    Past-perfect framing with no explicit return-to-present cue reads as more of
    the same recollection, so a contiguous memory keeps its block.
    """
    if _RESUME_RE.search(text):
        return False
    return bool(_PAST_PERFECT_RE.search(text))


@dataclass(frozen=True)
class TimeCue:
    """The classifier's verdict for one beat: its position + the matched marker."""

    position: TimePosition
    marker: str | None


def classify_time_position(text: str) -> TimeCue:
    """Classify a single beat's position on the story timeline from its cues.

    A back-cue wins over a forward-cue when both appear (memory framing dominates
    a passing "would" auxiliary); timeless cues only fire when no shift is found.
    """
    back = _BACK_RE.search(text)
    fwd = _FWD_RE.search(text)
    if back:
        return TimeCue(TimePosition.FLASHBACK, back.group(0).strip())
    if fwd:
        return TimeCue(TimePosition.FLASHFORWARD, fwd.group(0).strip())
    tl = _TIMELESS_RE.search(text)
    if tl:
        return TimeCue(TimePosition.TIMELESS, tl.group(0).strip())
    return TimeCue(TimePosition.PRESENT, None)


@dataclass(frozen=True)
class TimedBeat:
    """A beat reduced to what timeline reconstruction needs: order + text."""

    beat_id: str
    narrative_order: int
    text: str


@dataclass(frozen=True)
class ReconstructedBeat:
    """A beat after timeline reconstruction: its position, story rank, marker."""

    beat_id: str
    narrative_order: int
    position: TimePosition
    story_order: int
    marker: str | None


def reconstruct_timeline(beats: Sequence[TimedBeat]) -> list[ReconstructedBeat]:
    """Assign a story-time rank to each beat, honouring flashback/forward cues.

    Algorithm (conservative, stable):

    * Walk beats in narrative order maintaining a "present cursor".
    * A FLASHBACK beat is ranked *before* the present cursor (earlier story-time);
      a contiguous run of flashback beats keeps its internal narrative order but
      sits as a block just before where the flashback was entered.
    * A FLASHFORWARD beat is ranked *after* everything currently known.
    * PRESENT / TIMELESS beats advance the present cursor in reading order.
    * Returning to the present (explicit resume cue or a PRESENT beat after a
      flashback run) closes the block.

    Ranks are floats internally, then densified to ``0..n-1`` ints. Ties keep
    narrative order. The result never reorders the present line among itself.
    """
    cues = [classify_time_position(b.text) for b in beats]
    # Floating ranks let us interleave a flashback block "just before" the present
    # beat that the flashback is a memory *of* — i.e. before the last now-line beat.
    ranks: list[float] = [0.0] * len(beats)
    present_cursor = 0.0
    last_present_rank = -1.0  # rank of the most recent now-line beat (-1 = none yet)
    flashback_anchor: float | None = None
    flashback_step = 0
    forward_tail = float(len(beats)) * 2.0

    resolved_positions: list[TimePosition] = []
    for i, (beat, cue) in enumerate(zip(beats, cues, strict=True)):
        pos = cue.position

        # A PRESENT-cued beat inside an OPEN flashback that is still recounting
        # the past (past-perfect, no resume cue) is reclassified FLASHBACK — the
        # contiguous memory keeps its block until an explicit return to the now.
        if (
            pos is TimePosition.PRESENT
            and flashback_anchor is not None
            and _continues_flashback(beat.text)
        ):
            pos = TimePosition.FLASHBACK

        if pos is TimePosition.FLASHBACK:
            if flashback_anchor is None:
                # Anchor the block just before the present moment it recalls; if no
                # present beat has been seen yet, anchor before the timeline start.
                flashback_anchor = last_present_rank - 0.5
                flashback_step = 0
            ranks[i] = flashback_anchor + flashback_step * 1e-3
            flashback_step += 1
            resolved_positions.append(TimePosition.FLASHBACK)
            continue

        if pos is TimePosition.FLASHFORWARD:
            ranks[i] = forward_tail
            forward_tail += 1.0
            if bool(_RESUME_RE.search(beat.text)):
                flashback_anchor = None
            resolved_positions.append(TimePosition.FLASHFORWARD)
            continue

        # PRESENT or TIMELESS: on the now-line; closes any open flashback block.
        flashback_anchor = None
        ranks[i] = present_cursor
        last_present_rank = present_cursor
        present_cursor += 1.0
        resolved_positions.append(pos)

    resolved_cues = [
        TimeCue(position=p, marker=cues[i].marker) for i, p in enumerate(resolved_positions)
    ]
    return _densify(beats, resolved_cues, ranks)


def _densify(
    beats: Sequence[TimedBeat],
    cues: Sequence[TimeCue],
    ranks: Sequence[float],
) -> list[ReconstructedBeat]:
    """Map float ranks to dense 0..n-1 ints, breaking ties by narrative order."""
    order = sorted(range(len(beats)), key=lambda i: (ranks[i], beats[i].narrative_order))
    story_order = [0] * len(beats)
    for dense, original in enumerate(order):
        story_order[original] = dense
    return [
        ReconstructedBeat(
            beat_id=b.beat_id,
            narrative_order=b.narrative_order,
            position=cues[i].position,
            story_order=story_order[i],
            marker=cues[i].marker,
        )
        for i, b in enumerate(beats)
    ]


def in_story_order(beats: Sequence[ReconstructedBeat]) -> list[ReconstructedBeat]:
    """Return the beats sorted into chronological story order (for replay/recap)."""
    return sorted(beats, key=lambda b: (b.story_order, b.narrative_order))


def is_linear(beats: Sequence[ReconstructedBeat]) -> bool:
    """Whether story order equals narrative order (no flashbacks/forwards)."""
    return all(b.story_order == idx for idx, b in enumerate(beats))


def shift_replace(beat: ReconstructedBeat, **changes: object) -> ReconstructedBeat:
    """Convenience: a typed ``dataclasses.replace`` for a reconstructed beat."""
    return replace(beat, **changes)  # type: ignore[arg-type]


__all__ = [
    "ReconstructedBeat",
    "TimeCue",
    "TimedBeat",
    "classify_time_position",
    "in_story_order",
    "is_linear",
    "reconstruct_timeline",
]
