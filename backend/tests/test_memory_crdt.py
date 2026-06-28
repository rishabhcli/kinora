"""CRDT-law tests for the concurrent-write core (kinora.md §8) — no infra needed.

These prove the *distributed* behavior of the canon's concurrent-edit machinery offline: a
CvRDT is correct iff ``merge`` is commutative, associative, and idempotent, so merging the
same set of states in any order converges to one value. We assert exactly that for every
CRDT, plus the HLC monotonicity that makes the LWW order deterministic.
"""

from __future__ import annotations

import itertools

from app.memory.crdt import (
    HLC,
    GCounter,
    HLCClock,
    LWWRegister,
    ORSet,
    Stamp,
    VersionVector,
    all_orderings_agree,
)

# --- HLC monotonicity -------------------------------------------------------- #


def test_hlc_tick_is_monotone_even_when_physical_clock_stalls() -> None:
    hlc = HLC(100, 0)
    # Physical clock advances → adopt it, reset counter.
    nxt = hlc.tick(200)
    assert nxt == HLC(200, 0) and nxt > hlc
    # Physical clock stalls (same ms) → counter bumps so causality still advances.
    stalled = nxt.tick(200)
    assert stalled == HLC(200, 1) and stalled > nxt
    # Physical clock goes backwards (skew) → keep wall, bump counter; never regress.
    skewed = stalled.tick(150)
    assert skewed == HLC(200, 2) and skewed > stalled


def test_hlc_receive_dominates_both_inputs() -> None:
    local = HLC(100, 5)
    remote = HLC(100, 9)
    merged = local.receive(remote, now_ms=100)
    assert merged > local and merged > remote  # strictly dominates both


def test_hlc_clock_issues_strictly_increasing_stamps() -> None:
    ticks = iter([10, 10, 10, 11])
    clock = HLCClock("actor_a", now_ms=lambda: next(ticks))
    stamps = [clock.issue() for _ in range(4)]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == 4  # all distinct


# --- LWW-Register ------------------------------------------------------------ #


def _stamp(wall: int, counter: int = 0, actor: str = "a") -> Stamp:
    return Stamp(HLC(wall, counter), actor)


def test_lww_register_keeps_higher_stamp() -> None:
    a = LWWRegister("red", _stamp(1))
    b = a.set("blue", _stamp(2))
    assert b.value == "blue"
    # A stale write (lower stamp) is ignored.
    c = b.set("green", _stamp(1))
    assert c.value == "blue"


def test_lww_register_merge_laws() -> None:
    states = [
        LWWRegister("a", _stamp(1, actor="x")),
        LWWRegister("b", _stamp(2, actor="y")),
        LWWRegister("c", _stamp(2, actor="z")),  # tie on wall → actor breaks it
    ]
    assert all_orderings_agree(states, lambda p, q: p.merge(q))
    # Idempotence.
    for s in states:
        assert s.merge(s) == s


def test_lww_actor_tiebreak_is_total_and_deterministic() -> None:
    # Two writes with an identical HLC on different replicas resolve by actor_id.
    p = LWWRegister("from_x", _stamp(5, actor="x"))
    q = LWWRegister("from_z", _stamp(5, actor="z"))
    assert p.merge(q) == q.merge(p)  # commutative
    assert p.merge(q).value == "from_z"  # 'z' > 'x'


# --- OR-Set ------------------------------------------------------------------ #


def test_orset_add_remove_basic() -> None:
    s = ORSet[str]().add("elsa", "t1").add("hero", "t2")
    assert s.elements() == {"elsa", "hero"}
    s = s.remove("elsa")
    assert s.elements() == {"hero"}


def test_orset_concurrent_add_wins_over_remove() -> None:
    # Replica A removes 'sword' (observing tag t1); replica B concurrently re-adds it (t2).
    base = ORSet[str]().add("sword", "t1")
    replica_a = base.remove("sword")  # tombstones t1
    replica_b = base.add("sword", "t2")  # fresh tag, never observed by A's remove
    merged = replica_a.merge(replica_b)
    assert "sword" in merged.elements()  # add-wins: the re-assertion survives


def test_orset_merge_laws() -> None:
    states = [
        ORSet[str]().add("a", "t1"),
        ORSet[str]().add("b", "t2").remove("b"),
        ORSet[str]().add("a", "t3").add("c", "t4"),
    ]
    assert all_orderings_agree(states, lambda p, q: p.merge(q))
    for s in states:
        assert s.merge(s) == s


# --- G-Counter --------------------------------------------------------------- #


def test_gcounter_value_and_merge() -> None:
    a = GCounter().increment("x", 3).increment("y", 1)
    b = GCounter().increment("x", 1).increment("z", 5)
    assert a.value() == 4
    merged = a.merge(b)
    # Per-actor max, then sum: x=max(3,1)=3, y=1, z=5 → 9.
    assert merged.value() == 9
    assert merged.merge(merged) == merged  # idempotent


# --- Version Vector ---------------------------------------------------------- #


def test_version_vector_dominance_and_concurrency() -> None:
    base = VersionVector().observe("x").observe("y")
    fast_fwd = base.observe("x")  # only x advanced
    assert fast_fwd.dominates(base)
    assert not base.dominates(fast_fwd)
    assert not fast_fwd.concurrent_with(base)

    diverged = base.observe("y")  # y advanced on another line
    assert fast_fwd.concurrent_with(diverged)  # neither dominates → needs a merge rule
    lub = fast_fwd.merge(diverged)
    assert lub.dominates(fast_fwd) and lub.dominates(diverged)


def test_version_vector_merge_is_lattice_join() -> None:
    vvs = [
        VersionVector({"x": 2, "y": 1}),
        VersionVector({"x": 1, "z": 3}),
        VersionVector({"y": 4}),
    ]
    # Join (elementwise max) is order-independent.
    results = [
        VersionVector().merge(a).merge(b).merge(c)
        for a, b, c in itertools.permutations(vvs)
    ]
    assert all(r.clock == results[0].clock for r in results)
    assert results[0].clock == {"x": 2, "y": 4, "z": 3}
