"""Unit tests for non-linear timeline reconstruction (no network).

Narrative-time vs story-time: flashbacks rank earlier, flash-forwards later,
present line keeps reading order, ambiguous prose stays linear.
"""

from __future__ import annotations

from app.agents.comprehension.timeline import (
    TimedBeat,
    classify_time_position,
    in_story_order,
    is_linear,
    reconstruct_timeline,
)
from app.agents.contracts import TimePosition


def test_classify_flashback() -> None:
    c = classify_time_position("Years before, she had walked these same halls as a child.")
    assert c.position is TimePosition.FLASHBACK
    assert c.marker


def test_classify_flashforward() -> None:
    c = classify_time_position("She would later regret that choice for the rest of her days.")
    assert c.position is TimePosition.FLASHFORWARD


def test_classify_timeless_habitual() -> None:
    c = classify_time_position("Every morning she fed the chickens before dawn.")
    assert c.position is TimePosition.TIMELESS


def test_classify_present_default() -> None:
    c = classify_time_position("She crossed the courtyard and knocked on the heavy door.")
    assert c.position is TimePosition.PRESENT


def _beats(*texts: str) -> list[TimedBeat]:
    return [TimedBeat(beat_id=f"b{i}", narrative_order=i, text=t) for i, t in enumerate(texts)]


def test_linear_story_keeps_order() -> None:
    beats = _beats(
        "She woke at dawn.",
        "She walked to the market.",
        "She bought bread and went home.",
    )
    recon = reconstruct_timeline(beats)
    assert is_linear(recon)
    assert [b.story_order for b in recon] == [0, 1, 2]


def test_flashback_ranks_before_present() -> None:
    beats = _beats(
        "She sat by the fire now.",  # present (order ~0)
        "Years before, she had fled the burning village.",  # flashback (earlier)
        "She returned to the present and stirred the embers.",  # present resume
    )
    recon = reconstruct_timeline(beats)
    by_id = {b.beat_id: b for b in recon}
    # The flashback's story_order is earlier than the present beats around it.
    assert by_id["b1"].position is TimePosition.FLASHBACK
    assert by_id["b1"].story_order < by_id["b0"].story_order
    # Narrative order is untouched (scroll-sync key).
    assert [b.narrative_order for b in recon] == [0, 1, 2]
    # Not linear: story order diverges from narrative order.
    assert not is_linear(recon)


def test_flashforward_ranks_after() -> None:
    beats = _beats(
        "He boarded the train.",
        "Years later, he would never forget that platform.",  # flash-forward
        "The whistle blew and the train pulled away.",
    )
    recon = reconstruct_timeline(beats)
    by_id = {b.beat_id: b for b in recon}
    assert by_id["b1"].position is TimePosition.FLASHFORWARD
    # The flash-forward is chronologically last.
    assert by_id["b1"].story_order == max(b.story_order for b in recon)


def test_in_story_order_replays_chronologically() -> None:
    beats = _beats(
        "She lit the candle now.",
        "Long ago, she had been happy here.",
        "She blew the candle out.",
    )
    recon = reconstruct_timeline(beats)
    ordered = in_story_order(recon)
    # The flashback (b1) replays before the present beats.
    assert ordered[0].beat_id == "b1"


def test_contiguous_flashback_block_keeps_internal_order() -> None:
    beats = _beats(
        "She stood at the grave now.",
        "Years before, they had met at the fair.",  # flashback start
        "He had bought her a ribbon.",  # flashback continues (present-tense-ish, but in block)
        "She remembered his laugh.",  # still memory
        "Now she turned and walked away.",  # present resume
    )
    recon = reconstruct_timeline(beats)
    by_id = {b.beat_id: b for b in recon}
    # The flashback block (b1..b3) all rank before the closing present beat b4,
    # and keep their internal narrative order.
    assert by_id["b1"].story_order < by_id["b2"].story_order < by_id["b3"].story_order
    assert by_id["b3"].story_order < by_id["b4"].story_order
