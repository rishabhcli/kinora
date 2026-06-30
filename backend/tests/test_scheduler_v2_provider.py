"""Multi-provider concurrency-aware promotion policy tests (§4.9/§12.2).

Pure, no infra. Pins :func:`app.scheduler.v2.provider.plan_promotions`: it never
promotes past the free slots, fans across providers, prefers the soonest-landing
slot, honours the hard parallel cap, respects lane compatibility, routes around
unhealthy providers, and serialises a second shot on the same provider lane.
"""

from __future__ import annotations

from app.scheduler.v2.provider import (
    Lane,
    PromotionCandidate,
    ProviderState,
    covers_drain,
    plan_promotions,
    total_free_slots,
)


def _cands(n: int, *, committed: bool = True) -> list[PromotionCandidate]:
    return [
        PromotionCandidate(
            shot_id=f"shot_{i}", est_duration_s=5.0, eta_s=float(i * 5), committed=committed
        )
        for i in range(n)
    ]


# --- never invents capacity --------------------------------------------------- #


def test_no_providers_defers_everything() -> None:
    plan = plan_promotions(_cands(3), [])
    assert plan.promoted == 0
    assert len(plan.deferred) == 3


def test_no_candidates_is_empty_plan() -> None:
    plan = plan_promotions([], [ProviderState(name="p", free_committed=4)])
    assert plan.promoted == 0
    assert plan.deferred == []


def test_promotes_at_most_free_slots() -> None:
    providers = [ProviderState(name="p", free_committed=2, latency_s=10.0)]
    plan = plan_promotions(_cands(5), providers)
    assert plan.promoted == 2  # only 2 free slots
    assert len(plan.deferred) == 3


def test_fans_across_two_providers() -> None:
    providers = [
        ProviderState(name="a", free_committed=2, latency_s=10.0),
        ProviderState(name="b", free_committed=2, latency_s=10.0),
    ]
    plan = plan_promotions(_cands(4), providers)
    assert plan.promoted == 4
    assert len(plan.for_provider("a")) == 2
    assert len(plan.for_provider("b")) == 2


# --- soonest-landing preference ---------------------------------------------- #


def test_prefers_faster_provider_first() -> None:
    providers = [
        ProviderState(name="slow", free_committed=2, latency_s=20.0),
        ProviderState(name="fast", free_committed=2, latency_s=8.0),
    ]
    plan = plan_promotions(_cands(2), providers)
    # Both shots should land on the fast provider's slots first (8s < 20s).
    assert {a.provider for a in plan.assignments} == {"fast"}
    assert all(a.expected_landing_s == 8.0 for a in plan.assignments)


def test_free_slots_run_in_parallel() -> None:
    providers = [ProviderState(name="solo", free_committed=2, latency_s=10.0)]
    plan = plan_promotions(_cands(2), providers)
    landings = sorted(a.expected_landing_s for a in plan.assignments)
    # Both free slots start immediately and land at one latency (parallel).
    assert landings == [10.0, 10.0]
    # Only 2 free slots: a third candidate is deferred (no over-commit / queueing).
    plan3 = plan_promotions(_cands(3), providers)
    assert plan3.promoted == 2
    assert len(plan3.deferred) == 1


# --- hard parallel cap -------------------------------------------------------- #


def test_max_parallel_caps_fanout() -> None:
    providers = [ProviderState(name="p", free_committed=8, latency_s=10.0)]
    plan = plan_promotions(_cands(8), providers, max_parallel=3)
    assert plan.promoted == 3
    assert len(plan.deferred) == 5


# --- lane compatibility ------------------------------------------------------- #


def test_committed_candidate_needs_committed_slot() -> None:
    # Only speculative slots free → a committed candidate cannot be placed.
    providers = [ProviderState(name="p", free_committed=0, free_speculative=2, latency_s=10.0)]
    plan = plan_promotions(_cands(2, committed=True), providers)
    assert plan.promoted == 0
    assert len(plan.deferred) == 2


def test_speculative_candidate_uses_speculative_slot() -> None:
    providers = [ProviderState(name="p", free_committed=0, free_speculative=2, latency_s=10.0)]
    plan = plan_promotions(_cands(2, committed=False), providers)
    assert plan.promoted == 2
    assert all(a.lane is Lane.SPECULATIVE for a in plan.assignments)


def test_mixed_lanes_route_independently() -> None:
    providers = [ProviderState(name="p", free_committed=1, free_speculative=1, latency_s=10.0)]
    cands = [
        PromotionCandidate(shot_id="c", est_duration_s=5.0, eta_s=5.0, committed=True),
        PromotionCandidate(shot_id="s", est_duration_s=5.0, eta_s=5.0, committed=False),
    ]
    plan = plan_promotions(cands, providers)
    assert plan.promoted == 2
    assert len(plan.for_lane(Lane.COMMITTED)) == 1
    assert len(plan.for_lane(Lane.SPECULATIVE)) == 1


# --- health routing ----------------------------------------------------------- #


def test_unhealthy_provider_is_skipped() -> None:
    providers = [
        ProviderState(name="down", free_committed=4, latency_s=8.0, healthy=False),
        ProviderState(name="up", free_committed=2, latency_s=12.0, healthy=True),
    ]
    plan = plan_promotions(_cands(4), providers)
    assert plan.promoted == 2  # only the healthy provider's slots
    assert {a.provider for a in plan.assignments} == {"up"}


def test_total_free_slots_excludes_unhealthy() -> None:
    providers = [
        ProviderState(name="down", free_committed=4, healthy=False),
        ProviderState(name="up", free_committed=3, healthy=True),
    ]
    assert total_free_slots(providers, Lane.COMMITTED) == 3


# --- makespan + drain coverage ------------------------------------------------ #


def test_makespan_is_last_landing() -> None:
    providers = [ProviderState(name="solo", free_committed=2, latency_s=10.0)]
    # 4 candidates, 2 slots → 2 promoted this tick, both land at 10s.
    plan = plan_promotions(_cands(2), providers)
    assert plan.makespan_s == 10.0


def test_covers_drain_when_makespan_within_deadline() -> None:
    providers = [ProviderState(name="fast", free_committed=2, latency_s=8.0)]
    plan = plan_promotions(_cands(2), providers)
    assert covers_drain(plan, drain_deadline_s=12.0) is True
    assert covers_drain(plan, drain_deadline_s=5.0) is False


# --- determinism -------------------------------------------------------------- #


def test_plan_is_deterministic() -> None:
    providers = [
        ProviderState(name="a", free_committed=2, latency_s=10.0),
        ProviderState(name="b", free_committed=2, latency_s=14.0),
    ]
    p1 = plan_promotions(_cands(4), providers)
    p2 = plan_promotions(_cands(4), providers)
    assert [(a.shot_id, a.provider, a.lane) for a in p1.assignments] == [
        (a.shot_id, a.provider, a.lane) for a in p2.assignments
    ]
