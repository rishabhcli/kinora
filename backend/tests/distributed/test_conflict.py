"""Tests for conflict resolution (conflict.py) — the convergence-law contract."""

from __future__ import annotations

import itertools

import pytest

from app.distributed.replication.clock import HybridTimestamp, NodeId
from app.distributed.replication.conflict import (
    CustomResolver,
    GCounterValue,
    LWWResolver,
    MVRegisterValue,
    NonConvergentMergeError,
    ORSetValue,
    PNCounterValue,
    ResolverRegistry,
    Stamped,
)

A = NodeId("us", "a")
B = NodeId("eu", "b")
C = NodeId("ap", "c")


def ts(wall: int, node: NodeId = A, logical: int = 0) -> HybridTimestamp:
    return HybridTimestamp(wall, logical, node)


# --- LWW ------------------------------------------------------------------- #


def test_lww_keeps_higher_timestamp() -> None:
    r = LWWResolver[str]()
    lo = Stamped("old", ts(10))
    hi = Stamped("new", ts(20))
    assert r.resolve(lo, hi).value == "new"
    assert r.resolve(hi, lo).value == "new"  # commutative


def test_lww_total_order_breaks_wall_ties_by_node() -> None:
    r = LWWResolver[str]()
    # A = us/a, B = eu/b -> ordered by region: eu < us, so A > B.
    a = Stamped("a", ts(10, A))
    b = Stamped("b", ts(10, B))
    assert r.resolve(a, b).value == "a"
    assert r.resolve(b, a).value == "a"  # commutative


# --- counters -------------------------------------------------------------- #


def test_gcounter_sums_concurrent_increments() -> None:
    left = GCounterValue().increment(A, 3)
    right = GCounterValue().increment(B, 5)
    merged = left.merge(right)
    assert merged.value == 8


def test_gcounter_merge_is_idempotent() -> None:
    c = GCounterValue().increment(A, 2).increment(B, 1)
    assert c.merge(c).value == c.value
    assert c.merge(c).tallies == c.tallies


def test_gcounter_rejects_negative() -> None:
    with pytest.raises(ValueError):
        GCounterValue().increment(A, -1)


def test_pncounter_tracks_pos_and_neg() -> None:
    c = PNCounterValue().add(A, 10).add(B, -3)
    assert c.value == 7
    other = PNCounterValue().add(A, 10).add(C, -1)  # A increment is concurrent same-node
    merged = c.merge(other)
    # per-node max on positives: A=10 (not 20), C neg=1, B neg=3 -> 10 - 4 = 6
    assert merged.value == 6


# --- OR-set ---------------------------------------------------------------- #


def test_orset_add_wins_over_concurrent_remove() -> None:
    base = ORSetValue[str]().add("x", "tag-old")
    removed = base.remove("x")  # tombstones tag-old
    re_added = base.add("x", "tag-new")  # concurrent fresh tag
    merged = removed.merge(re_added)
    assert merged.contains("x")  # add-wins


def test_orset_sequential_remove_after_observing_add() -> None:
    s = ORSetValue[str]().add("x", "t1")
    s = s.remove("x")
    assert not s.contains("x")


def test_orset_merge_idempotent() -> None:
    s = ORSetValue[str]().add("a", "t1").add("b", "t2").remove("b")
    assert s.merge(s).elements() == s.elements()


# --- MV register ----------------------------------------------------------- #


def test_mvregister_keeps_concurrent_siblings() -> None:
    reg = MVRegisterValue[str]()
    # two writes, each from a fresh empty register -> neither saw the other.
    left = reg.write("left", ts(10, A))
    right = reg.write("right", ts(10, B))
    merged = left.merge(right)
    assert merged.is_conflicted
    assert merged.values == {"left", "right"}


def test_mvregister_sequential_write_supersedes() -> None:
    reg = MVRegisterValue[str]()
    first = reg.write("first", ts(10, A))
    # second write observes `first`'s context, so it supersedes it (not concurrent).
    second = first.write("second", ts(20, A))
    assert not second.is_conflicted
    assert second.values == {"second"}


def test_mvregister_dominating_write_collapses_concurrent_siblings() -> None:
    reg = MVRegisterValue[str]()
    conflicted = reg.write("left", ts(10, A)).merge(reg.write("right", ts(10, B)))
    assert conflicted.is_conflicted
    # a write on the merged (conflicted) register has seen both dots -> collapses.
    resolved = conflicted.write("winner", ts(20, C))
    assert not resolved.is_conflicted
    assert resolved.values == {"winner"}


def test_mvregister_merge_idempotent_and_commutative() -> None:
    reg = MVRegisterValue[str]()
    left = reg.write("left", ts(10, A))
    right = reg.write("right", ts(10, B))
    assert left.merge(right).values == right.merge(left).values
    merged = left.merge(right)
    assert merged.merge(merged).values == merged.values


# --- custom ---------------------------------------------------------------- #


def test_custom_resolver_merges_sets() -> None:
    union = CustomResolver(lambda a, b: a | b, label="union")
    assert union.resolve({1, 2}, {2, 3}) == {1, 2, 3}
    assert union.name() == "custom:union"


def test_custom_resolver_flags_non_commutative_merge() -> None:
    # subtraction is not commutative.
    bad = CustomResolver(lambda a, b: a - b, label="minus")
    with pytest.raises(NonConvergentMergeError):
        bad.resolve(5, 3)


def test_custom_resolver_flags_non_idempotent_merge() -> None:
    bad = CustomResolver(lambda a, b: a + b, label="sum")
    with pytest.raises(NonConvergentMergeError):
        bad.resolve(1, 1)


# --- registry -------------------------------------------------------------- #


def test_registry_longest_prefix_wins() -> None:
    reg = ResolverRegistry(default=LWWResolver())
    summing: CustomResolver[int] = CustomResolver(
        lambda a, b: a + b, label="sum", strict=False
    )
    reg.register("counter:", summing)
    assert reg.for_key("counter:likes").name().startswith("custom")
    assert isinstance(reg.for_key("scene:42"), LWWResolver)


def test_registry_unbound_key_raises_without_default() -> None:
    reg = ResolverRegistry()
    with pytest.raises(KeyError):
        reg.for_key("anything")


# --- law sweeps ------------------------------------------------------------ #

_LAW_CASES = [
    ("gcounter", [GCounterValue().increment(n, i + 1) for i, n in enumerate([A, B, C])]),
    (
        "orset",
        [
            ORSetValue[str]().add("x", "t1"),
            ORSetValue[str]().add("y", "t2").remove("y"),
            ORSetValue[str]().add("x", "t3"),
        ],
    ),
    (
        "mvreg",
        [
            MVRegisterValue[str]().write("a", ts(10, A)),
            MVRegisterValue[str]().write("b", ts(10, B)),
            MVRegisterValue[str]().write("c", ts(15, C)),
        ],
    ),
]


@pytest.mark.parametrize("label,values", _LAW_CASES, ids=[c[0] for c in _LAW_CASES])
def test_merge_is_commutative(label: str, values: list) -> None:
    a, b, _ = values
    assert a.merge(b) == b.merge(a)


@pytest.mark.parametrize("label,values", _LAW_CASES, ids=[c[0] for c in _LAW_CASES])
def test_merge_is_associative(label: str, values: list) -> None:
    a, b, c = values
    assert a.merge(b).merge(c) == a.merge(b.merge(c))


@pytest.mark.parametrize("label,values", _LAW_CASES, ids=[c[0] for c in _LAW_CASES])
def test_merge_is_idempotent(label: str, values: list) -> None:
    for v in values:
        assert v.merge(v) == v


@pytest.mark.parametrize("label,values", _LAW_CASES, ids=[c[0] for c in _LAW_CASES])
def test_all_orderings_converge(label: str, values: list) -> None:
    """Folding the same values in every order yields one identical result."""
    results = set()
    for perm in itertools.permutations(values):
        acc = perm[0]
        for v in perm[1:]:
            acc = acc.merge(v)
        results.add(acc)
    assert len(results) == 1
