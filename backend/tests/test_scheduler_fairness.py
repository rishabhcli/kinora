"""Multi-reader fairness tests (kinora.md §12.2/§11.1) — pure, no infra.

Pin :class:`app.scheduler.fairness.FairShareAllocator`: a plentiful pool satisfies
everyone; a scarce pool is split max-min so no reader starves; weights bias the
split; per-session sub-caps bound a single reader; satisfied sessions free their
share (work-conserving); the total allocated never exceeds the pool.
"""

from __future__ import annotations

from app.scheduler.fairness import (
    Allocation,
    FairShareAllocator,
    SessionDemand,
)


def _alloc(min_share: float = 0.1) -> FairShareAllocator:
    return FairShareAllocator(min_share_fraction=min_share)


def test_plentiful_pool_satisfies_every_session() -> None:
    demands = [
        SessionDemand("a", deficit_s=20.0),
        SessionDemand("b", deficit_s=30.0),
        SessionDemand("c", deficit_s=10.0),
    ]
    out: Allocation = _alloc().allocate(demands, pool_s=1000.0)
    assert out.cap_for("a") == 20.0
    assert out.cap_for("b") == 30.0
    assert out.cap_for("c") == 10.0


def test_scarce_pool_never_exceeds_pool() -> None:
    demands = [SessionDemand(s, deficit_s=50.0) for s in ("a", "b", "c", "d")]
    out = _alloc().allocate(demands, pool_s=40.0)
    assert out.total_s <= 40.0 + 1e-6


def test_scarce_pool_does_not_starve_a_small_reader() -> None:
    # One huge-deficit reader + one small one share a tight pool. The small reader
    # must still get a guaranteed floor (max-min), not zero.
    demands = [
        SessionDemand("hog", deficit_s=1000.0),
        SessionDemand("small", deficit_s=5.0),
    ]
    out = _alloc(min_share=0.2).allocate(demands, pool_s=20.0)
    assert out.cap_for("small") > 0.0
    assert out.total_s <= 20.0 + 1e-6


def test_weights_bias_the_split() -> None:
    demands = [
        SessionDemand("vip", deficit_s=100.0, weight=3.0),
        SessionDemand("std", deficit_s=100.0, weight=1.0),
    ]
    out = _alloc(min_share=0.0).allocate(demands, pool_s=40.0)
    # 3:1 weighting → roughly 30 vs 10.
    assert out.cap_for("vip") > out.cap_for("std")
    assert abs(out.cap_for("vip") - 30.0) < 2.0
    assert abs(out.cap_for("std") - 10.0) < 2.0


def test_per_session_cap_bounds_a_single_reader() -> None:
    demands = [
        SessionDemand("a", deficit_s=200.0, per_session_cap_s=15.0),
        SessionDemand("b", deficit_s=200.0),
    ]
    out = _alloc(min_share=0.0).allocate(demands, pool_s=100.0)
    assert out.cap_for("a") <= 15.0 + 1e-6
    # b absorbs the rest (work-conserving), bounded by the pool.
    assert out.cap_for("b") <= 100.0 - out.cap_for("a") + 1e-6


def test_satisfied_sessions_free_their_share() -> None:
    # 'tiny' needs only 2s; the rest of the pool flows to 'big'.
    demands = [
        SessionDemand("tiny", deficit_s=2.0),
        SessionDemand("big", deficit_s=1000.0),
    ]
    out = _alloc(min_share=0.0).allocate(demands, pool_s=50.0)
    assert out.cap_for("tiny") == 2.0
    assert abs(out.cap_for("big") - 48.0) < 1e-3


def test_no_demand_allocates_nothing() -> None:
    demands = [SessionDemand("a", deficit_s=0.0), SessionDemand("b", deficit_s=0.0)]
    out = _alloc().allocate(demands, pool_s=100.0)
    assert out.total_s == 0.0


def test_empty_pool_allocates_nothing() -> None:
    demands = [SessionDemand("a", deficit_s=50.0)]
    out = _alloc().allocate(demands, pool_s=0.0)
    assert out.cap_for("a") == 0.0


def test_allocation_is_deterministic() -> None:
    demands = [SessionDemand(s, deficit_s=float(20 + i * 7)) for i, s in enumerate("abcde")]
    a = _alloc().allocate(demands, pool_s=63.0)
    b = _alloc().allocate(demands, pool_s=63.0)
    assert a.caps == b.caps
