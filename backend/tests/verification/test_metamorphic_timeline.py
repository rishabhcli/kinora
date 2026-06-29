"""Metamorphic + property tests for §4.2 timeline reconstruction.

``reconstruct_timeline`` separates *narrative-time* (reading order — the
scroll-sync key that must never change) from *story-time* (chronological order,
which flashbacks/forwards reshuffle). The structural invariants below are exactly
the guarantees the source-span index and any "story-order recap" rely on:

* **story_order is a permutation of 0..n-1** (densification is a bijection — no
  rank is lost or duplicated);
* **narrative_order is preserved verbatim** (reconstruction never touches the
  scroll-sync key);
* **the PRESENT line keeps its relative order** (flashbacks move *around* the
  now-line, never reorder it among itself) — the load-bearing §4.2 MR;
* **a flashback ranks before the present beat it precedes**, a flashforward after;
* **a purely-present run is linear** (story order == narrative order).
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from app.agents.comprehension.timeline import (
    classify_time_position,
    in_story_order,
    is_linear,
    reconstruct_timeline,
)
from app.agents.contracts import TimePosition
from app.verification.properties.relations import (
    present_line_ids_in_story_order,
    timed_from_texts,
)

# A vocabulary of cue-bearing and neutral beat texts, so generated runs exercise
# the classifier's real branches (flashback / forward / timeless / present).
BACK_CUES = ["years before", "she remembered", "long ago", "had vanished once"]
FWD_CUES = ["years later", "she would later see", "someday", "little did she know"]
TIMELESS_CUES = ["every morning", "used to walk", "on sundays"]
PRESENT_TEXTS = ["she opened the door", "the rain fell", "he spoke", "a bell rang", ""]

cue_texts = st.sampled_from(BACK_CUES + FWD_CUES + TIMELESS_CUES + PRESENT_TEXTS)
present_texts = st.sampled_from(PRESENT_TEXTS)


@st.composite
def text_runs(draw: st.DrawFn, *, min_size: int = 0, max_size: int = 12) -> list[str]:
    return draw(st.lists(cue_texts, min_size=min_size, max_size=max_size))


@given(text_runs())
def test_story_order_is_a_permutation(texts: list[str]) -> None:
    """Densified story ranks are exactly ``{0, ..., n-1}`` — a clean bijection."""
    beats = reconstruct_timeline(timed_from_texts(texts))
    orders = sorted(b.story_order for b in beats)
    assert orders == list(range(len(texts)))


@given(text_runs())
def test_narrative_order_is_preserved(texts: list[str]) -> None:
    """The scroll-sync key (narrative_order) is never disturbed by reconstruction."""
    beats = reconstruct_timeline(timed_from_texts(texts))
    assert [b.narrative_order for b in beats] == list(range(len(texts)))
    # And the beats come back in narrative order (the input order).
    assert [b.beat_id for b in beats] == [f"b{i}" for i in range(len(texts))]


@given(text_runs())
def test_present_line_keeps_relative_order(texts: list[str]) -> None:
    """Metamorphic (§4.2): the PRESENT line is never reordered among itself.

    Flashbacks/forwards are ranked around the now-line, so the PRESENT beats —
    listed in story order — must appear in the same relative order they had in the
    narration (ascending narrative index ⇒ ascending story order).
    """
    beats = reconstruct_timeline(timed_from_texts(texts))
    present = [b for b in beats if b.position is TimePosition.PRESENT]
    # narrative order of the present beats, and their story order, must agree.
    by_narrative = sorted(present, key=lambda b: b.narrative_order)
    by_story = sorted(present, key=lambda b: b.story_order)
    assert [b.beat_id for b in by_narrative] == [b.beat_id for b in by_story]


@given(present_line_only=st.lists(present_texts, min_size=1, max_size=10))
def test_pure_present_run_is_linear(present_line_only: list[str]) -> None:
    """A run with no shift cues reconstructs to story-order == narrative-order."""
    beats = reconstruct_timeline(timed_from_texts(present_line_only))
    # All present (the neutral texts carry no cue).
    assert all(b.position is TimePosition.PRESENT for b in beats)
    assert is_linear(beats)
    assert [b.story_order for b in beats] == list(range(len(beats)))


@given(text_runs(min_size=1))
def test_in_story_order_is_a_stable_total_order(texts: list[str]) -> None:
    """``in_story_order`` yields strictly ascending story ranks (a total order)."""
    beats = reconstruct_timeline(timed_from_texts(texts))
    ordered = in_story_order(beats)
    ranks = [b.story_order for b in ordered]
    assert ranks == sorted(ranks)
    # It's a reordering of the same beats — no loss/duplication.
    assert {b.beat_id for b in ordered} == {b.beat_id for b in beats}


def test_flashback_ranks_before_the_present_it_recalls() -> None:
    """A flashback beat sits earlier in story-time than the present beat after it."""
    texts = ["she opened the door", "years before, the house burned", "she wept now"]
    beats = reconstruct_timeline(timed_from_texts(texts))
    by_id = {b.beat_id: b for b in beats}
    # b1 is the flashback; b0/b2 are present.
    assert by_id["b1"].position is TimePosition.FLASHBACK
    assert by_id["b1"].story_order < by_id["b2"].story_order


def test_flashforward_ranks_after_everything() -> None:
    """A flash-forward beat is ranked after the present beats around it."""
    texts = ["she packed her bag", "years later she would return", "she left the house"]
    beats = reconstruct_timeline(timed_from_texts(texts))
    by_id = {b.beat_id: b for b in beats}
    assert by_id["b1"].position is TimePosition.FLASHFORWARD
    assert by_id["b1"].story_order > by_id["b0"].story_order
    assert by_id["b1"].story_order > by_id["b2"].story_order


@given(st.text(max_size=40))
def test_classify_is_total(text: str) -> None:
    """The single-beat classifier returns a valid position for any text (no crash)."""
    cue = classify_time_position(text)
    assert cue.position in set(TimePosition)


@given(text_runs())
def test_reconstruction_is_deterministic(texts: list[str]) -> None:
    a = reconstruct_timeline(timed_from_texts(texts))
    b = reconstruct_timeline(timed_from_texts(texts))
    assert [(x.beat_id, x.story_order, x.position) for x in a] == [
        (y.beat_id, y.story_order, y.position) for y in b
    ]


@given(text_runs())
def test_present_line_invariant_helper_agrees(texts: list[str]) -> None:
    """The relations helper and the story-order sort agree on the present line."""
    beats = reconstruct_timeline(timed_from_texts(texts))
    helper = present_line_ids_in_story_order(beats)
    present = sorted(
        (b for b in beats if b.position is TimePosition.PRESENT),
        key=lambda b: b.narrative_order,
    )
    assert helper == [b.beat_id for b in present]
