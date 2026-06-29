"""Metamorphic-relation helpers shared across the metamorphic test suite.

A *metamorphic relation* (MR) is a rule of the form "if I transform the input
this way, the output must change this way (or not at all)". They catch bugs an
example test can't, because they don't need a known-good oracle — only a known
*relationship*. These helpers are the input transforms; the assertions live in the
test modules.

The relations Kinora's policy core should satisfy:

* **Velocity scaling (§4.6).** Multiplying reading velocity by ``k > 1`` divides
  every ETA by ``k`` — so a faster reader can only pull a shot *toward* committed,
  never push it away. Zone classification is therefore monotone in velocity.
* **Translation invariance (§4.3).** Shifting the focus word and a shot's start by
  the same delta leaves the ETA (and zone) unchanged — only the *gap* matters.
* **Threshold monotonicity (§9.5).** Improving any QA axis (raising CCS, lowering
  drift/motion) can never turn a PASS into a FAIL.
* **Beat reordering (§4.2).** Reordering *independent* beats (different pages) must
  not change which beats pack together page-by-page; reordering the present-line
  must preserve the present-line's relative story order.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.comprehension.timeline import ReconstructedBeat, TimedBeat
from app.agents.contracts import Beat


def scale_gap(focus_word: int, start: int, k: float) -> tuple[int, int]:
    """Return ``(focus, start')`` such that the *gap* ``start-focus`` is scaled by ``k``.

    Keeps ``focus_word`` fixed and moves the shot start so the new gap is ``k`` ×
    the old gap (rounded to an int). Used to test that ETA scales as ``1/v`` by
    instead scaling the numerator — same effect on ETA, integer-clean.
    """
    gap = start - focus_word
    return focus_word, focus_word + round(gap * k)


def shift_positions(focus_word: int, start: int, delta: int) -> tuple[int, int]:
    """Translate both the focus word and the shot start by ``delta`` (gap-preserving)."""
    return focus_word + delta, start + delta


def improve_qa(
    ccs: float, style_drift: float, motion: float, *, eps: float = 0.01
) -> tuple[float, float, float]:
    """Strictly improve every numeric QA axis (clamped to valid ranges).

    Raises CCS, lowers style-drift and motion-artifact by ``eps`` — a transform
    that can only make a clip *better*, so a passing clip must stay passing.
    """
    return (
        min(1.0, ccs + eps),
        max(0.0, style_drift - eps),
        max(0.0, motion - eps),
    )


def degrade_qa(
    ccs: float, style_drift: float, motion: float, *, eps: float = 0.01
) -> tuple[float, float, float]:
    """Strictly worsen every numeric QA axis (the dual of :func:`improve_qa`)."""
    return (
        max(0.0, ccs - eps),
        min(1.0, style_drift + eps),
        min(1.0, motion + eps),
    )


def reverse(beats: Sequence[Beat]) -> list[Beat]:
    """Reverse a beat run (the strongest reordering perturbation)."""
    return list(reversed(beats))


def to_timed(beats: Sequence[Beat]) -> list[TimedBeat]:
    """Project a Beat run onto the timeline reconstructor's ``TimedBeat`` input."""
    return [
        TimedBeat(beat_id=b.beat_id or f"b{i}", narrative_order=i, text=b.summary or "")
        for i, b in enumerate(beats)
    ]


def timed_from_texts(texts: Sequence[str]) -> list[TimedBeat]:
    """Build a TimedBeat run straight from texts in narrative order."""
    return [TimedBeat(beat_id=f"b{i}", narrative_order=i, text=t) for i, t in enumerate(texts)]


def present_line_ids_in_story_order(
    reconstructed: Sequence[ReconstructedBeat],
) -> list[str]:
    """The PRESENT-line beats' ids, listed in ascending story order.

    Used to assert the §4.2 invariant: the present line is *never* reordered among
    itself by timeline reconstruction (flashbacks/forwards move around it).
    """
    from app.agents.contracts import TimePosition

    present = [b for b in reconstructed if b.position is TimePosition.PRESENT]
    present.sort(key=lambda b: b.story_order)
    return [b.beat_id for b in present]


__all__ = [
    "degrade_qa",
    "improve_qa",
    "present_line_ids_in_story_order",
    "reverse",
    "scale_gap",
    "shift_positions",
    "timed_from_texts",
    "to_timed",
]
