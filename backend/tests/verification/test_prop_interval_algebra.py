"""Property tests for the §8.5 Allen interval algebra (``BeatInterval``).

The continuity engine reasons about fact lifetimes via the thirteen Allen
relations, so this small unit must satisfy the classical algebra laws. The
strongest check is a **ground-truth cross-check**: for finite intervals we
materialise the actual sets of beats and verify the symbolic relation agrees with
the concrete set relationship (overlap, containment, equality). This catches a
mis-stated endpoint comparison that a purely symbolic test would miss.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from app.render.continuity_reasoning.intervals import (
    OVERLAP_RELATIONS,
    Allen,
    BeatInterval,
    inverse,
)
from app.verification.properties.strategies import beat_coords, beat_intervals

ALL_RELATIONS = list(Allen)


def _beat_set(iv: BeatInterval, *, cap: int = 40) -> set[int]:
    """The concrete set of beats a *finite* interval is active at (half-open)."""
    end = iv.end if iv.end is not None else cap
    return set(range(iv.start, end))


# --------------------------------------------------------------------------- #
# inverse — an involution / bijection on the 13 relations
# --------------------------------------------------------------------------- #


@given(st.sampled_from(ALL_RELATIONS))
def test_inverse_is_an_involution(rel: Allen) -> None:
    """``inverse(inverse(r)) == r`` for every relation."""
    assert inverse(inverse(rel)) is rel


def test_inverse_is_a_bijection_on_all_relations() -> None:
    """``inverse`` permutes the 13 relations (no relation is dropped or doubled)."""
    images = {inverse(r) for r in ALL_RELATIONS}
    assert images == set(ALL_RELATIONS)


def test_equals_is_the_only_self_inverse() -> None:
    self_inverse = {r for r in ALL_RELATIONS if inverse(r) is r}
    assert self_inverse == {Allen.EQUALS}


# --------------------------------------------------------------------------- #
# relate — totality, reflexivity, converse-symmetry
# --------------------------------------------------------------------------- #


@given(beat_intervals(), beat_intervals())
def test_relate_returns_exactly_one_relation(a: BeatInterval, b: BeatInterval) -> None:
    """Totality: ``relate`` always returns a valid single Allen relation."""
    assert a.relate(b) in set(Allen)


@given(beat_intervals())
def test_relate_is_reflexive(a: BeatInterval) -> None:
    """An interval equals itself."""
    assert a.relate(a) is Allen.EQUALS


@given(beat_intervals(), beat_intervals())
def test_relate_converse_symmetry(a: BeatInterval, b: BeatInterval) -> None:
    """The defining law: if ``A r B`` then ``B inverse(r) A`` (§8.5)."""
    assert b.relate(a) is inverse(a.relate(b))


@given(beat_intervals(), beat_intervals())
def test_equals_iff_same_endpoints(a: BeatInterval, b: BeatInterval) -> None:
    """``EQUALS`` holds exactly when both bounds coincide (∞ included)."""
    equals = a.relate(b) is Allen.EQUALS
    same = (a.start == b.start) and (a.end == b.end)
    # Two open intervals with the same start are equal (both run to +∞).
    same_open = a.start == b.start and a.is_open and b.is_open
    assert equals == (same or same_open)


# --------------------------------------------------------------------------- #
# overlaps — symmetric, reflexive on non-empty, ground-truth cross-check
# --------------------------------------------------------------------------- #


@given(beat_intervals(), beat_intervals())
def test_overlaps_is_symmetric(a: BeatInterval, b: BeatInterval) -> None:
    assert a.overlaps(b) == b.overlaps(a)


@given(beat_intervals())
def test_overlaps_is_reflexive_on_nonempty(a: BeatInterval) -> None:
    """A non-empty interval overlaps itself (an empty one shares no beat)."""
    assume(a.is_open or a.end != a.start)
    assert a.overlaps(a)


@given(beat_intervals(), beat_intervals())
def test_overlap_relation_matches_shared_beats(
    a: BeatInterval, b: BeatInterval
) -> None:
    """Ground truth: the symbolic overlap matches the concrete shared-beat set.

    For *non-empty* finite intervals (and capped open ones), ``a.overlaps(b)`` must
    be exactly "the two beat sets intersect". This binds the symbolic algebra to
    the half-open semantics it claims to model.
    """
    # Empty intervals (start == end) contain no beats; the algebra's
    # interior-overlap classification is a separate, intentional convention there.
    assume(a.is_open or a.end != a.start)
    assume(b.is_open or b.end != b.start)
    shared = bool(_beat_set(a) & _beat_set(b))
    assert a.overlaps(b) == shared


@given(beat_intervals(), beat_intervals())
def test_overlaps_consistent_with_relation_set(
    a: BeatInterval, b: BeatInterval
) -> None:
    """``overlaps`` is precisely membership of the relation in OVERLAP_RELATIONS."""
    assert a.overlaps(b) == (a.relate(b) in OVERLAP_RELATIONS)


def test_overlap_relations_set_is_inverse_closed() -> None:
    """The overlap set is closed under ``inverse`` (overlap is symmetric)."""
    for rel in OVERLAP_RELATIONS:
        assert inverse(rel) in OVERLAP_RELATIONS


# --------------------------------------------------------------------------- #
# contains_beat — half-open membership
# --------------------------------------------------------------------------- #


@given(beat_intervals(), beat_coords)
def test_contains_beat_is_half_open(a: BeatInterval, beat: int) -> None:
    """``contains_beat`` is exactly ``start <= beat < end`` (∞ for open)."""
    end = a.end
    expected = a.start <= beat and (end is None or beat < end)
    assert a.contains_beat(beat) == expected


@given(beat_intervals())
def test_start_is_contained_iff_nonempty(a: BeatInterval) -> None:
    """The start beat is active iff the interval isn't empty."""
    nonempty = a.is_open or a.end != a.start
    assert a.contains_beat(a.start) == nonempty


@given(beat_intervals())
def test_end_is_never_contained(a: BeatInterval) -> None:
    """The (exclusive) end beat is never active — the retirement beat is excluded."""
    end = a.end
    if end is not None:
        assert not a.contains_beat(end)


@given(beat_coords, beat_coords)
def test_before_means_disjoint_with_a_gap(start: int, length: int) -> None:
    """``A BEFORE B`` ⇒ no shared beat and A's end strictly precedes B's start."""
    length = abs(length)
    a = BeatInterval(start, start + length)
    b = BeatInterval(start + length + 1, start + length + 3)
    assert a.relate(b) is Allen.BEFORE
    assert not a.overlaps(b)


def test_post_init_rejects_inverted_intervals() -> None:
    """An end before its start is rejected at construction (a malformed lifetime)."""
    import pytest

    with pytest.raises(ValueError, match="precedes start"):
        BeatInterval(start=10, end=5)
