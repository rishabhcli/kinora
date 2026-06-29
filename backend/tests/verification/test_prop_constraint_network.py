"""Property tests for §8.5 temporal constraint propagation (composition + AllenNetwork).

The continuity engine detects "this shot contradicts an active state" by building a
constraint network of fact-lifetime intervals and running path-consistency: an
empty edge means no ordering satisfies all constraints (a contradiction). The
correctness of that contradiction detection rests on the Allen *composition*
algebra and the network's propagation. The headline property is a soundness
guarantee:

* **A network built from concrete intervals is always path-consistent** — real
  intervals have a model (the integers), so the propagator must never report a
  false contradiction on a realisable timeline. (False *positives* here would
  block legitimate shots; this catches them.)

plus the algebra laws (EQUALS identity, converse symmetry, composition realised by
a witness third interval) and the network's structural invariants (converse
symmetry, singleton edges from concrete intervals).
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from app.render.continuity_reasoning.composition import (
    ALL_RELATIONS,
    compose,
    compose_singletons,
    converse,
)
from app.render.continuity_reasoning.constraints import AllenNetwork
from app.render.continuity_reasoning.intervals import Allen, BeatInterval, inverse
from app.verification.properties.strategies import beat_intervals

ALL = list(Allen)
relations = st.sampled_from(ALL)


# --------------------------------------------------------------------------- #
# Composition algebra
# --------------------------------------------------------------------------- #


@given(relations)
def test_equals_is_two_sided_identity(r: Allen) -> None:
    """``EQUALS ∘ r == {r}`` and ``r ∘ EQUALS == {r}`` (the identity element)."""
    assert compose_singletons(Allen.EQUALS, r) == frozenset({r})
    assert compose_singletons(r, Allen.EQUALS) == frozenset({r})


@given(relations, relations)
def test_composition_cells_are_nonempty(r1: Allen, r2: Allen) -> None:
    """Every composition is realisable — no empty cell (the grid covers all 169)."""
    assert compose_singletons(r1, r2)


@given(relations, relations)
def test_composition_converse_law(r1: Allen, r2: Allen) -> None:
    """The algebra law: ``converse(r1 ∘ r2) == converse(r2) ∘ converse(r1)``."""
    lhs = converse(compose_singletons(r1, r2))
    rhs = compose(converse(frozenset({r2})), converse(frozenset({r1})))
    assert lhs == rhs


@given(st.sets(relations, min_size=1), st.sets(relations, min_size=1))
def test_compose_is_union_of_singletons(r1: set[Allen], r2: set[Allen]) -> None:
    """Set composition is exactly the union of its members' single compositions."""
    expected: set[Allen] = set()
    for a in r1:
        for b in r2:
            expected |= compose_singletons(a, b)
    assert compose(frozenset(r1), frozenset(r2)) == frozenset(expected)


@given(st.sets(relations, min_size=1))
def test_converse_is_an_involution_on_sets(r: set[Allen]) -> None:
    assert converse(converse(frozenset(r))) == frozenset(r)


def test_composition_realised_by_a_witness_triple() -> None:
    """Ground truth: for concrete *non-empty* A,B,C, ``A relate C`` ∈ ``compose(A→B, B→C)``.

    This is the defining soundness property of the table — the relation actually
    observed between A and C must be a member of the composed set.

    NOTE (MINOR-3, DESIGN.md): the table is built from non-empty intervals only, so
    it is *unsound when a degenerate empty interval* ``[n, n)`` mediates the
    composition — e.g. ``A=[0,1)``, ``B=[1,1)``, ``C=[1,2)`` gives
    ``A relate C = MEETS`` but ``compose(MEETS, MEETS) = {BEFORE}``. Empty intervals
    are pinned separately; here we test the table's real (non-empty) domain.
    """
    from itertools import product

    intervals = [BeatInterval(s, e) for s in range(5) for e in range(s + 1, 6)]
    intervals += [BeatInterval(s, None) for s in range(5)]
    for a, b, c in product(intervals, repeat=3):
        composed = compose_singletons(a.relate(b), b.relate(c))
        assert a.relate(c) in composed


def test_composition_table_is_unsound_for_empty_intervals_MINOR3() -> None:
    """Regression pin for MINOR-3: an empty mediating interval breaks composition soundness.

    The §8.5 composition table is computed only over non-empty intervals, so a
    degenerate zero-width fact lifetime ``[1,1)`` can mediate a chain the table does
    not realise. ``BeatInterval`` permits ``start == end`` (only ``end < start`` is
    rejected), so this is constructible. ``pytest.fail`` flips the pin if the table
    is ever extended to cover empty intervals.
    """
    import pytest

    a, b, c = BeatInterval(0, 1), BeatInterval(1, 1), BeatInterval(1, 2)
    composed = compose_singletons(a.relate(b), b.relate(c))
    if a.relate(c) in composed:
        pytest.fail("empty-interval composition gap MINOR-3 appears fixed — un-pin this test")
    assert a.relate(c) is Allen.MEETS
    assert composed == frozenset({Allen.BEFORE})


# --------------------------------------------------------------------------- #
# AllenNetwork — structure + propagation soundness
# --------------------------------------------------------------------------- #


@st.composite
def interval_maps(draw: st.DrawFn, *, nonempty: bool = False) -> dict[str, BeatInterval]:
    n = draw(st.integers(min_value=1, max_value=5))
    out: dict[str, BeatInterval] = {}
    for i in range(n):
        iv = draw(beat_intervals())
        if nonempty and not iv.is_open and iv.end == iv.start:
            # Widen a degenerate empty interval to a unit one for the non-empty domain.
            iv = BeatInterval(iv.start, iv.start + 1)
        out[f"iv{i}"] = iv
    return out


@given(interval_maps(nonempty=True))
def test_concrete_nonempty_network_is_path_consistent(
    intervals: dict[str, BeatInterval],
) -> None:
    """SOUNDNESS: a network of real *non-empty* intervals never reports a contradiction.

    Non-empty intervals are jointly realisable on the integer line, so
    path-consistency must always succeed — a failure here would be a *false*
    continuity contradiction blocking a legitimate shot. (The empty-interval case is
    a known unsoundness, BUG-2 / MINOR-3 — pinned separately below.)
    """
    net = AllenNetwork.from_intervals(intervals)
    result = net.path_consistency()
    assert result.consistent
    assert not result.empty


def test_empty_interval_network_can_false_contradict_BUG2() -> None:
    """Regression pin for BUG-2: an empty interval can trigger a FALSE contradiction.

    ``A=[0,1)``, ``B=[1,1)`` (zero-width), ``C=[1,2)`` is perfectly realisable, yet
    path-consistency collapses an edge and reports inconsistent — because the §8.5
    composition table (built from non-empty intervals only, MINOR-3) mis-composes
    ``MEETS ∘ MEETS`` through the empty mediator. In production this would wrongly
    flag a legitimate shot as a timeline violation. ``pytest.fail`` flips the pin if
    the table/interval model is taught to handle zero-width lifetimes.
    """
    import pytest

    intervals = {"A": BeatInterval(0, 1), "B": BeatInterval(1, 1), "C": BeatInterval(1, 2)}
    result = AllenNetwork.from_intervals(intervals).path_consistency()
    if result.consistent:
        pytest.fail("empty-interval false-contradiction BUG-2 appears fixed — un-pin this test")
    assert result.empty


@given(interval_maps())
def test_concrete_network_relations_match_ground_truth(
    intervals: dict[str, BeatInterval],
) -> None:
    """Each network edge is the singleton relation the two intervals actually stand in."""
    net = AllenNetwork.from_intervals(intervals)
    names = list(intervals)
    for i in names:
        for j in names:
            if i == j:
                assert net.relation(i, j) == frozenset({Allen.EQUALS})
            else:
                assert net.relation(i, j) == frozenset({intervals[i].relate(intervals[j])})


@given(interval_maps())
def test_network_is_converse_symmetric(
    intervals: dict[str, BeatInterval],
) -> None:
    """``edges[(i,j)] == converse(edges[(j,i)])`` — the network's stated invariant."""
    net = AllenNetwork.from_intervals(intervals)
    names = list(intervals)
    for i in names:
        for j in names:
            assert net.relation(i, j) == converse(net.relation(j, i))


@given(interval_maps())
def test_path_consistency_preserves_converse_symmetry(
    intervals: dict[str, BeatInterval],
) -> None:
    """Propagation keeps the network converse-symmetric (tightening both directions)."""
    net = AllenNetwork.from_intervals(intervals)
    net.path_consistency()
    names = list(intervals)
    for i in names:
        for j in names:
            assert net.relation(i, j) == converse(net.relation(j, i))


def test_contradictory_constraints_are_detected() -> None:
    """A hand-asserted impossible cycle collapses an edge → inconsistent + a trace.

    A BEFORE B, B BEFORE C, but C BEFORE A is unsatisfiable (a temporal cycle);
    path-consistency must find the empty edge and carry a proof trace.
    """
    net = AllenNetwork()
    net.constrain("A", "B", frozenset({Allen.BEFORE}))
    net.constrain("B", "C", frozenset({Allen.BEFORE}))
    net.constrain("C", "A", frozenset({Allen.BEFORE}))
    result = net.path_consistency()
    assert not result.consistent
    assert result.empty
    assert result.trace is not None
    assert result.trace.contradiction


def test_constrain_tightens_monotonically() -> None:
    """Repeated ``constrain`` intersects — the edge only ever shrinks."""
    net = AllenNetwork()
    net.constrain("A", "B", frozenset({Allen.BEFORE, Allen.MEETS}))
    assert net.relation("A", "B") == frozenset({Allen.BEFORE, Allen.MEETS})
    net.constrain("A", "B", frozenset({Allen.BEFORE}))
    assert net.relation("A", "B") == frozenset({Allen.BEFORE})
    # Converse kept in lockstep.
    assert net.relation("B", "A") == frozenset({inverse(Allen.BEFORE)})


def test_unconstrained_pairs_default_to_universal() -> None:
    """A fresh network with no edges returns the universal relation for any pair."""
    net = AllenNetwork()
    net.add_node("x")
    net.add_node("y")
    assert net.relation("x", "y") == ALL_RELATIONS
