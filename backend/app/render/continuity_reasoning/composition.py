"""The Allen composition table, computed by definition (for temporal reasoning).

Composition answers: given ``A r1 B`` and ``B r2 C``, what relation(s) can hold
between ``A`` and ``C``? The answer is a *set* of Allen relations — the constraint
may not pin it to one. A path-consistency network (``constraints.py``) iterates
this operation to a fixpoint to detect *implied* contradictions (e.g. A before B,
B before C, but C before A is unsatisfiable).

Rather than hand-transcribe Allen's 13×13 table (169 error-prone cells), we
**compute** it by its definition: over a small integer grid, enumerate every
triple of intervals ``(A, B, C)``; whenever ``A r1 B`` and ``B r2 C`` are
observed, record ``A relate C``. A grid spanning enough distinct endpoint
orderings realises every composition exactly. This reuses the already-tested
:meth:`BeatInterval.relate`, so the table is correct by construction, not by
careful typing. The result is cached at import.

Pure and deterministic; :data:`ALL_RELATIONS` is the universal (no-information)
relation used to seed a network.
"""

from __future__ import annotations

from functools import cache
from itertools import combinations, product

from .intervals import Allen, BeatInterval, inverse

#: A disjunctive set of possible Allen relations between two intervals.
RelationSet = frozenset[Allen]

#: The universal relation set — "anything is possible" (network seed).
ALL_RELATIONS: RelationSet = frozenset(Allen)


def _build_table() -> dict[tuple[Allen, Allen], frozenset[Allen]]:
    """Compute the composition table by enumerating concrete interval triples.

    A grid of 6 endpoints (0..5) yields every relative ordering of three
    intervals' four endpoints needed to realise all 13×13 compositions. For each
    triple we observe ``(A relate B, B relate C)`` and union in ``A relate C``.
    """
    points = range(6)
    intervals = [BeatInterval(s, e) for s, e in combinations(points, 2)]
    table: dict[tuple[Allen, Allen], set[Allen]] = {
        (r1, r2): set() for r1 in Allen for r2 in Allen
    }
    for a, b, c in product(intervals, repeat=3):
        r1 = a.relate(b)
        r2 = b.relate(c)
        table[(r1, r2)].add(a.relate(c))
    # Open-ended intervals (end=None) extend the grid's reach for relations that
    # only arise at +∞; fold them in so every cell is fully populated.
    open_intervals = [BeatInterval(s, None) for s in points]
    pool = intervals + open_intervals
    for a, b, c in product(pool, repeat=3):
        r1 = a.relate(b)
        r2 = b.relate(c)
        table[(r1, r2)].add(a.relate(c))
    return {key: frozenset(val) for key, val in table.items()}


_TABLE: dict[tuple[Allen, Allen], frozenset[Allen]] = _build_table()


def converse(rel_set: RelationSet) -> RelationSet:
    """The converse of a relation set: invert every member."""
    return frozenset(inverse(r) for r in rel_set)


@cache
def compose(r1: RelationSet, r2: RelationSet) -> RelationSet:
    """Compose two relation *sets*: the union of single compositions.

    ``compose({BEFORE}, {BEFORE}) == {BEFORE}`` (before is transitive);
    ``compose({BEFORE}, {AFTER}) == ALL_RELATIONS`` (no information). This is the
    operation a path-consistency network applies along each triangle.
    """
    out: set[Allen] = set()
    for a in r1:
        for b in r2:
            out |= _TABLE[(a, b)]
    return frozenset(out)


def compose_singletons(r1: Allen, r2: Allen) -> RelationSet:
    """Compose two single relations (one raw table entry)."""
    return _TABLE[(r1, r2)]


def _self_check() -> None:
    """Verify the computed table's algebraic invariants at import (fail fast)."""
    assert len(_TABLE) == 169, f"composition table has {len(_TABLE)} entries, expected 169"
    # Every cell must be non-empty (the grid realises every composition).
    for key, val in _TABLE.items():
        assert val, f"empty composition cell at {key}"
    # EQUALS is the two-sided identity.
    for r in Allen:
        assert _TABLE[(Allen.EQUALS, r)] == frozenset({r}), f"e∘{r} != {{{r}}}"
        assert _TABLE[(r, Allen.EQUALS)] == frozenset({r}), f"{r}∘e != {{{r}}}"
    # Transitive singletons.
    assert _TABLE[(Allen.BEFORE, Allen.BEFORE)] == frozenset({Allen.BEFORE})
    assert _TABLE[(Allen.AFTER, Allen.AFTER)] == frozenset({Allen.AFTER})
    assert _TABLE[(Allen.DURING, Allen.DURING)] == frozenset({Allen.DURING})
    assert _TABLE[(Allen.MEETS, Allen.MEETS)] == frozenset({Allen.BEFORE})
    assert _TABLE[(Allen.BEFORE, Allen.AFTER)] == ALL_RELATIONS
    # Converse law: (r1 ∘ r2)⁻¹ == r2⁻¹ ∘ r1⁻¹ for every pair.
    for r1 in Allen:
        for r2 in Allen:
            lhs = converse(_TABLE[(r1, r2)])
            rhs = compose(frozenset({inverse(r2)}), frozenset({inverse(r1)}))
            assert lhs == rhs, f"converse law fails at {r1}∘{r2}"


_self_check()


__all__ = [
    "ALL_RELATIONS",
    "RelationSet",
    "compose",
    "compose_singletons",
    "converse",
]
