"""Speculative pre-generation engine tests (kinora.md §4.4/§4.6/§4.8) — pure, no infra.

Every test here is deterministic over a **virtual clock + a synthetic reader trace**:
no network, no Redis, no real provider, KINORA_LIVE_VIDEO never touched. They pin
the engine's six guarantees:

* probability-weighted reach selection (linear decay + jump mass),
* an EV portfolio that is *budget-bounded* and maximises expected hit value,
* cheap-model-for-low-probability / premium-reserved-for-high routing,
* path-invalidation cancellation with exactly-once refund of unstarted work,
* cache salvage of cancelled-but-re-hittable assets,
* a hit/waste accounting loop that tunes aggressiveness,
* and the hard invariant that the engine NEVER spends past the speculative cap.
"""

from __future__ import annotations

import pytest

from app.video.speculate import (
    EngineConfig,
    InMemorySpeculativeBudget,
    ModelClass,
    PathKind,
    PlannerPolicy,
    PortfolioPlanner,
    ReachModel,
    ReachPredictor,
    ReaderState,
    RoutingPolicy,
    SetCache,
    SpeculationAccountant,
    SpeculationChoice,
    SpeculationEngine,
    SpeculationLedger,
    TieredCostModel,
    TunerPolicy,
    UpcomingShot,
    class_for_probability,
    route_model_for_probability,
)
from app.video.speculate.cost import ModelSpec
from app.video.speculate.predictor import _eta_seconds
from app.video.speculate.types import PredictedReach

# --------------------------------------------------------------------------- #
# Virtual clock + synthetic reader trace helpers
# --------------------------------------------------------------------------- #


class VirtualClock:
    """A hand-driven monotonic clock (seconds). Tests advance it explicitly."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _shots(
    n: int,
    *,
    start: int = 100,
    step: int = 100,
    seconds: float = 5.0,
    jump_targets: set[int] | None = None,
) -> list[UpcomingShot]:
    jt = jump_targets or set()
    return [
        UpcomingShot(
            shot_key=f"s{i}",
            word_start=start + i * step,
            video_seconds=seconds,
            is_jump_target=(i in jt),
        )
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #
# 1) Reach predictor — probability-weighted, branching paths
# --------------------------------------------------------------------------- #


def test_linear_probability_decays_with_distance() -> None:
    """A shot further ahead has a strictly lower linear hit-probability."""
    pred = ReachPredictor(ReachModel(linear_horizon_s=60.0))
    state = ReaderState(focus_word=0, velocity_wps=4.0)
    shots = _shots(5, start=40, step=200)  # 10s, 60s, 110s, ... ahead at 4 wps
    reaches = pred.predict(state, shots)
    by_key = {r.shot.shot_key: r for r in reaches}
    # Closer shot has higher probability than a farther one.
    assert by_key["s0"].hit_probability > by_key["s1"].hit_probability
    assert by_key["s1"].hit_probability > by_key["s2"].hit_probability
    # All are linear (no jump targets) and ETA increases with distance.
    assert all(r.kind is PathKind.LINEAR for r in reaches)
    assert by_key["s0"].eta_s < by_key["s2"].eta_s


def test_jump_target_gets_jump_probability_mass() -> None:
    """A far shot flagged as a jump target beats an equally-far non-target."""
    pred = ReachPredictor(
        ReachModel(
            linear_horizon_s=60.0,
            jump_base_rate=0.3,
            jump_dwell_ms_scale=200.0,
            min_hit_probability=0.01,
        )
    )
    # Long dwell ⇒ the jump-rate multiplier saturates upward (deliberate nav).
    state = ReaderState(focus_word=0, velocity_wps=4.0, dwell_ms=400.0)
    # Two shots equally far (linear prob ~ identical, but small); only one is a
    # jump target, so jump mass should lift it well above the plain one.
    shots = [
        UpcomingShot(shot_key="far_plain", word_start=720, video_seconds=5.0),
        UpcomingShot(
            shot_key="far_jump", word_start=720, video_seconds=5.0, is_jump_target=True
        ),
    ]
    reaches = {r.shot.shot_key: r for r in pred.predict(state, shots)}
    assert reaches["far_jump"].hit_probability > reaches["far_plain"].hit_probability
    assert reaches["far_jump"].kind is PathKind.JUMP


def test_unsteady_reader_discounts_linear_reach() -> None:
    """An unsteady skimmer's linear forecast is trusted less (§4.6 gate)."""
    pred = ReachPredictor(ReachModel(unsteady_linear_penalty=0.5))
    shots = _shots(3, start=200, step=200)
    steady = pred.predict(ReaderState(focus_word=0, velocity_wps=4.0, steady=True), shots)
    unsteady = pred.predict(
        ReaderState(focus_word=0, velocity_wps=4.0, steady=False), shots
    )
    s_by = {r.shot.shot_key: r.hit_probability for r in steady}
    u_by = {r.shot.shot_key: r.hit_probability for r in unsteady}
    for key in s_by:
        assert u_by[key] < s_by[key]


def test_reaches_are_proper_probabilities_and_sorted() -> None:
    """Every hit-probability is in [0,1] and the list is descending by prob."""
    pred = ReachPredictor()
    state = ReaderState(focus_word=0, velocity_wps=4.0)
    reaches = pred.predict(state, _shots(8, start=40, step=120))
    assert all(0.0 <= r.hit_probability <= 1.0 for r in reaches)
    probs = [r.hit_probability for r in reaches]
    assert probs == sorted(probs, reverse=True)


def test_backward_shots_are_not_predicted() -> None:
    """Shots behind the reader are dropped (forward-only forecast)."""
    pred = ReachPredictor()
    state = ReaderState(focus_word=500, velocity_wps=4.0)
    shots = [
        UpcomingShot(shot_key="behind", word_start=100, video_seconds=5.0),
        UpcomingShot(shot_key="ahead", word_start=600, video_seconds=5.0),
    ]
    keys = {r.shot.shot_key for r in pred.predict(state, shots)}
    assert keys == {"ahead"}


# --------------------------------------------------------------------------- #
# 2) Cost model + probability→model routing
# --------------------------------------------------------------------------- #


def test_routing_reserves_premium_for_high_probability() -> None:
    """Low prob → cheap turbo; high prob → premium (kinora.md §4.6)."""
    cost = TieredCostModel.default()
    assert class_for_probability(0.05) is ModelClass.CHEAP
    assert class_for_probability(0.5) is ModelClass.STANDARD
    assert class_for_probability(0.9) is ModelClass.PREMIUM
    assert cost.class_of(route_model_for_probability(cost, 0.05)) is ModelClass.CHEAP
    assert cost.class_of(route_model_for_probability(cost, 0.95)) is ModelClass.PREMIUM


def test_routing_thresholds_are_configurable() -> None:
    policy = RoutingPolicy(premium_probability=0.5, standard_probability=0.2)
    assert class_for_probability(0.6, policy) is ModelClass.PREMIUM
    assert class_for_probability(0.3, policy) is ModelClass.STANDARD
    assert class_for_probability(0.1, policy) is ModelClass.CHEAP


def test_routing_degrades_when_class_absent() -> None:
    """A table with only a cheap id still routes a high-prob shot to it."""
    cost = TieredCostModel(
        {
            "only": ModelSpec(
                model_id="only",
                model_class=ModelClass.CHEAP,
                usd_per_second=0.05,
                latency_per_second_s=1.0,
            )
        }
    )
    assert route_model_for_probability(cost, 0.99) == "only"


def test_cost_and_latency_scale_with_duration() -> None:
    cost = TieredCostModel.default()
    assert cost.cost_usd("turbo", 10.0) == pytest.approx(0.5)
    assert cost.latency_s("turbo", 10.0) == pytest.approx(4.0 + 2.0 * 10.0)
    # Premium is pricier and slower than turbo.
    assert cost.cost_usd("premium", 5.0) > cost.cost_usd("turbo", 5.0)
    assert cost.latency_s("premium", 5.0) > cost.latency_s("turbo", 5.0)


# --------------------------------------------------------------------------- #
# 3) EV portfolio planner — budget-bounded, value-maximising
# --------------------------------------------------------------------------- #


def _reach(key: str, prob: float, *, word: int, seconds: float = 5.0, value: float = 0.0,
           eta: float = 5.0) -> PredictedReach:
    return PredictedReach(
        shot=UpcomingShot(
            shot_key=key, word_start=word, video_seconds=seconds, value=value
        ),
        kind=PathKind.LINEAR,
        hit_probability=prob,
        eta_s=eta,
    )


def test_planner_never_exceeds_budget() -> None:
    """The selected portfolio's total cost is always <= the cap."""
    cost = TieredCostModel.default()
    planner = PortfolioPlanner(
        cost, PlannerPolicy(budget_usd=0.30, min_ev_per_dollar=0.0)
    )
    # Many high-prob shots (each premium ~0.30/sec * 5s = 1.50) — only ~0 fit.
    reaches = [_reach(f"s{i}", 0.9, word=100 + i * 10, eta=100.0) for i in range(10)]
    plan = planner.plan(reaches, budget_usd=0.30)
    assert plan.total_cost_usd <= 0.30 + 1e-9


def test_planner_prefers_higher_expected_value_within_budget() -> None:
    """Given two affordable shots and room for one, the higher-EV one is picked."""
    cost = TieredCostModel.default()
    # Force both to the cheap tier (low prob) so cost is equal; vary value.
    planner = PortfolioPlanner(
        cost, PlannerPolicy(budget_usd=0.25, min_ev_per_dollar=0.0)
    )
    low = _reach("low", 0.2, word=100, value=1.0, eta=100.0)
    high = _reach("high", 0.2, word=110, value=50.0, eta=100.0)
    # Each cheap 5s = 0.25; budget fits exactly one.
    plan = planner.plan([low, high], budget_usd=0.25)
    assert plan.shot_keys == ["high"]


def test_planner_drops_candidate_below_ev_floor() -> None:
    """A long-shot whose expected value can't beat its expected waste is skipped."""
    cost = TieredCostModel.default()
    planner = PortfolioPlanner(cost, PlannerPolicy(budget_usd=10.0, min_ev_per_dollar=1.0))
    # prob 0.2, value == duration 5 ⇒ EV 1.0; cheap cost 0.25 ⇒ ev/$ = 4.0 (passes).
    good = _reach("good", 0.2, word=100, eta=100.0)
    # prob 0.02, value 5 ⇒ EV 0.1; ev/$ = 0.4 (below floor 1.0) → dropped.
    bad = _reach("bad", 0.02, word=110, eta=100.0)
    plan = planner.plan([good, bad], budget_usd=10.0)
    assert "good" in plan.shot_keys
    assert "bad" not in plan.shot_keys
    assert any(reason == "below-ev-floor" for _, reason in plan.skipped)


def test_planner_rejects_render_that_cannot_beat_eta() -> None:
    """A shot whose render latency exceeds its ETA is infeasible and skipped."""
    cost = TieredCostModel.default()
    planner = PortfolioPlanner(cost, PlannerPolicy(budget_usd=10.0, min_ev_per_dollar=0.0))
    # Premium 5s latency = 10 + 8*5 = 50s; ETA only 5s ⇒ can't finish in time.
    too_late = _reach("late", 0.95, word=100, eta=5.0)
    plan = planner.plan([too_late], budget_usd=10.0)
    assert plan.shot_keys == []
    assert any(reason == "latency-exceeds-eta" for _, reason in plan.skipped)


def test_planner_skips_already_cached_shots() -> None:
    """A shot already in cache is a free hit — no spend planned for it."""
    cost = TieredCostModel.default()
    planner = PortfolioPlanner(cost, PlannerPolicy(budget_usd=10.0, min_ev_per_dollar=0.0))
    r = _reach("cached", 0.9, word=100, eta=100.0)
    plan = planner.plan([r], budget_usd=10.0, already_cached={"cached"})
    assert plan.shot_keys == []
    assert any(reason == "already-cached" for _, reason in plan.skipped)


def test_exact_knapsack_picks_highest_value_under_budget() -> None:
    """The exact DP fills the budget with the highest-value affordable shots."""
    cost = TieredCostModel.default()
    reaches = [_reach(f"s{i}", 0.2, word=100 + i, value=float(i + 1), eta=100.0)
               for i in range(6)]
    exact = PortfolioPlanner(
        cost, PlannerPolicy(budget_usd=0.75, min_ev_per_dollar=0.0)
    ).plan(reaches, budget_usd=0.75)
    # 0.75 / 0.25 (cheap 5s) = exactly 3 shots; the DP takes the 3 highest values.
    assert len(exact.selected) == 3
    assert set(exact.shot_keys) == {"s3", "s4", "s5"}


def test_greedy_fallback_stays_budget_bounded_on_large_input() -> None:
    """A large candidate set trips the greedy fallback and still respects the cap."""
    cost = TieredCostModel.default()
    # A coarse quantum + many candidates + a big budget pushes the DP cell count
    # over the exact-DP threshold, exercising the greedy path.
    planner = PortfolioPlanner(
        cost,
        PlannerPolicy(budget_usd=500.0, min_ev_per_dollar=0.0, cost_quantum_usd=0.001),
    )
    reaches = [_reach(f"s{i}", 0.3, word=100 + i, value=float(i % 7 + 1), eta=1000.0)
               for i in range(2000)]
    plan = planner.plan(reaches, budget_usd=500.0)
    assert plan.total_cost_usd <= 500.0 + 1e-6
    # Greedy should have taken many (each cheap 5s = 0.25 → up to 2000 fit in 500).
    assert len(plan.selected) > 0


# --------------------------------------------------------------------------- #
# 4) Cancellation ledger — invalidation, refund, salvage, idempotency
# --------------------------------------------------------------------------- #


def _choice(planner: PortfolioPlanner, reach: PredictedReach) -> SpeculationChoice:
    return planner.price(reach)


def test_invalidation_refunds_unstarted_reservation() -> None:
    """A forward jump cancels a now-unreachable PENDING shot and refunds it."""
    budget = InMemorySpeculativeBudget(10.0)
    cache = SetCache()
    ledger = SpeculationLedger(budget, cache)
    planner = PortfolioPlanner(TieredCostModel.default())

    choice = _choice(planner, _reach("s", 0.2, word=100, eta=100.0))
    budget.reserve(choice.cost_usd)
    ledger.register(choice)
    assert budget.remaining_usd() == pytest.approx(10.0 - choice.cost_usd)

    # Reader jumps far past s (keep band starts at 5000) → s is unreachable.
    outcome = ledger.invalidate(new_focus_word=5000, keep_horizon_words=1000)
    assert outcome.cancelled == ["s"]
    assert outcome.refunded_usd == pytest.approx(choice.cost_usd)
    # Refund returned to the pool.
    assert budget.remaining_usd() == pytest.approx(10.0)


def test_started_render_is_not_refunded_on_cancel() -> None:
    """A RUNNING render's seconds are sunk — cancel stops it but cannot refund."""
    budget = InMemorySpeculativeBudget(10.0)
    ledger = SpeculationLedger(budget, SetCache())
    planner = PortfolioPlanner(TieredCostModel.default())
    choice = _choice(planner, _reach("s", 0.2, word=100, eta=100.0))
    budget.reserve(choice.cost_usd)
    ledger.register(choice)
    ledger.mark_running("s")  # reservation → realised spend
    assert budget.reserved_usd == pytest.approx(0.0)
    assert budget.spent_usd == pytest.approx(choice.cost_usd)

    outcome = ledger.invalidate(new_focus_word=5000, keep_horizon_words=100)
    assert outcome.cancelled == ["s"]
    assert outcome.refunded_usd == pytest.approx(0.0)  # nothing to refund


def test_double_invalidation_does_not_double_refund() -> None:
    """Cancelling twice refunds exactly once (the §4.8 idempotency guard)."""
    budget = InMemorySpeculativeBudget(10.0)
    ledger = SpeculationLedger(budget, SetCache())
    planner = PortfolioPlanner(TieredCostModel.default())
    choice = _choice(planner, _reach("s", 0.2, word=100, eta=100.0))
    budget.reserve(choice.cost_usd)
    ledger.register(choice)

    first = ledger.invalidate(new_focus_word=9000, keep_horizon_words=10)
    second = ledger.invalidate(new_focus_word=9000, keep_horizon_words=10)
    assert first.refunded_usd == pytest.approx(choice.cost_usd)
    assert second.refunded_usd == pytest.approx(0.0)
    assert budget.remaining_usd() == pytest.approx(10.0)


def test_cancellation_salvages_cacheable_asset() -> None:
    """A backward-glance-likely shot is salvaged into cache on cancel (§4.8)."""
    budget = InMemorySpeculativeBudget(10.0)
    cache = SetCache(salvageable={"keep_me"})
    ledger = SpeculationLedger(budget, cache)
    planner = PortfolioPlanner(TieredCostModel.default())
    for key in ("keep_me", "drop_me"):
        c = _choice(planner, _reach(key, 0.2, word=100, eta=100.0))
        budget.reserve(c.cost_usd)
        ledger.register(c)
    outcome = ledger.invalidate(new_focus_word=9000, keep_horizon_words=10)
    assert set(outcome.cancelled) == {"keep_me", "drop_me"}
    assert outcome.salvaged == ["keep_me"]


def test_in_window_speculation_is_kept() -> None:
    """A shot the new trajectory still reaches survives invalidation (cache hit)."""
    budget = InMemorySpeculativeBudget(10.0)
    ledger = SpeculationLedger(budget, SetCache())
    planner = PortfolioPlanner(TieredCostModel.default())
    c = _choice(planner, _reach("s", 0.2, word=600, eta=100.0))
    budget.reserve(c.cost_usd)
    ledger.register(c)
    # New focus 500, keep band to 1500 → s at 600 is kept.
    outcome = ledger.invalidate(new_focus_word=500, keep_horizon_words=1000)
    assert outcome.kept == ["s"]
    assert outcome.cancelled == []
    assert budget.remaining_usd() == pytest.approx(10.0 - c.cost_usd)


def test_done_asset_is_salvaged_without_refund() -> None:
    """A finished (DONE) shot is salvaged but not refunded (already realised)."""
    budget = InMemorySpeculativeBudget(10.0)
    ledger = SpeculationLedger(budget, SetCache())
    planner = PortfolioPlanner(TieredCostModel.default())
    c = _choice(planner, _reach("s", 0.2, word=100, eta=100.0))
    budget.reserve(c.cost_usd)
    ledger.register(c)
    ledger.mark_done("s")
    outcome = ledger.invalidate(new_focus_word=9000, keep_horizon_words=10)
    assert outcome.salvaged == ["s"]
    assert outcome.refunded_usd == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 5) Accounting feedback loop — aggressiveness tuning
# --------------------------------------------------------------------------- #


def test_high_hit_rate_with_spare_budget_increases_aggressiveness() -> None:
    """Consistent hits + spare budget ⇒ tuner widens the horizon (> 1.0)."""
    acc = SpeculationAccountant(TunerPolicy(hit_target=0.5, waste_ceiling=0.5))
    for _ in range(12):
        acc.record_launch(0.25)
        acc.record_hit(cost_usd=0.25, value=5.0)
    factor = acc.aggressiveness(budget_utilisation=0.0)
    assert factor > 1.0


def test_high_waste_rate_decreases_aggressiveness() -> None:
    """Repeated sunk wastes ⇒ tuner pulls the horizon in (< 1.0)."""
    acc = SpeculationAccountant(TunerPolicy(hit_target=0.5, waste_ceiling=0.2))
    for _ in range(12):
        acc.record_launch(0.25)
        acc.record_waste(cost_usd=0.25, refunded_usd=0.0)  # fully sunk
    factor = acc.aggressiveness(budget_utilisation=0.0)
    assert factor < 1.0


def test_cold_start_aggressiveness_is_neutral() -> None:
    """A fresh session is neutral (1.0) — no spend regression vs baseline."""
    acc = SpeculationAccountant()
    assert acc.aggressiveness() == 1.0


def test_aggressiveness_is_bounded() -> None:
    """No record, however skewed, drives the multiplier outside its clamp band."""
    from app.video.speculate.accounting import MAX_AGGRESSIVENESS, MIN_AGGRESSIVENESS

    hot = SpeculationAccountant(TunerPolicy(gain=100.0, hit_target=0.0))
    for _ in range(20):
        hot.record_hit(cost_usd=0.1, value=10.0)
    assert hot.aggressiveness(budget_utilisation=0.0) <= MAX_AGGRESSIVENESS

    cold = SpeculationAccountant(TunerPolicy(gain=100.0, waste_ceiling=0.0))
    for _ in range(20):
        cold.record_waste(cost_usd=0.1, refunded_usd=0.0)
    assert cold.aggressiveness(budget_utilisation=0.0) >= MIN_AGGRESSIVENESS


def test_refunded_waste_is_not_counted_as_sunk() -> None:
    """A fully-refunded cancellation costs zero waste (matches the budget)."""
    acc = SpeculationAccountant()
    acc.record_launch(0.25)
    acc.record_waste(cost_usd=0.25, refunded_usd=0.25)
    assert acc.stats.wasted_usd == pytest.approx(0.0)
    assert acc.stats.refunded_usd == pytest.approx(0.25)


def test_stats_round_trip_json() -> None:
    """SpeculationStats is pydantic-serialisable for the persistence path."""
    acc = SpeculationAccountant()
    acc.record_launch(0.25)
    acc.record_hit(cost_usd=0.25, value=5.0)
    from app.video.speculate import SpeculationStats

    dumped = acc.stats.model_dump_json()
    restored = SpeculationStats.model_validate_json(dumped)
    assert restored.hits == 1
    assert restored.hit_value == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# 6) Engine — end-to-end over a virtual clock + reader trace
# --------------------------------------------------------------------------- #


def test_engine_consider_launches_within_budget() -> None:
    """The engine launches a budget-bounded portfolio and never overspends."""
    clock = VirtualClock()
    engine = SpeculationEngine(
        EngineConfig(
            speculative_budget_usd=0.50,
            planner=PlannerPolicy(budget_usd=0.50, min_ev_per_dollar=0.0),
        ),
        clock=clock,
    )
    state = ReaderState(focus_word=0, velocity_wps=4.0)
    result = engine.consider(state, _shots(10, start=40, step=120))
    assert engine.budget.remaining_usd() >= 0.0
    assert engine.budget.reserved_usd <= 0.50 + 1e-9
    # Something was launched (near shots are affordable).
    assert result.launched


def test_engine_hit_then_diverge_records_and_refunds() -> None:
    """A reader trace: launch ahead, hit one, then jump away → refund + waste tally."""
    clock = VirtualClock()
    engine = SpeculationEngine(
        EngineConfig(
            speculative_budget_usd=5.0,
            keep_horizon_words=500,
            planner=PlannerPolicy(budget_usd=5.0, min_ev_per_dollar=0.0),
        ),
        clock=clock,
    )
    state = ReaderState(focus_word=0, velocity_wps=4.0)
    result = engine.consider(state, _shots(6, start=100, step=150))
    assert result.launched
    launched_keys = result.launched_keys

    # Reader actually reaches the first launched shot (a hit).
    hit_key = launched_keys[0]
    engine.on_reader_arrived(hit_key)
    assert engine.accountant.stats.hits == 1

    spent_before = engine.budget.spent_usd
    reserved_before = engine.budget.reserved_usd
    # Reader jumps far away → every still-pending speculation is cancelled+refunded.
    outcome = engine.on_path_diverged(new_focus_word=100_000)
    assert outcome.cancelled
    # Refund returned reserved dollars to the pool.
    assert engine.budget.reserved_usd <= reserved_before
    assert outcome.refunded_usd >= 0.0
    # Spend never decreased (refunds touch reservations, not realised spend).
    assert engine.budget.spent_usd == pytest.approx(spent_before)
    # Waste was recorded for the cancellations.
    assert engine.accountant.stats.wastes == len(outcome.cancelled)


def test_engine_never_exceeds_budget_across_many_passes() -> None:
    """Repeated consider passes as the reader advances never breach the cap."""
    clock = VirtualClock()
    cap = 1.0
    engine = SpeculationEngine(
        EngineConfig(
            speculative_budget_usd=cap,
            planner=PlannerPolicy(budget_usd=cap, min_ev_per_dollar=0.0),
        ),
        clock=clock,
    )
    focus = 0
    for _ in range(20):
        state = ReaderState(focus_word=focus, velocity_wps=4.0)
        shots = [
            UpcomingShot(shot_key=f"w{focus + i * 100}", word_start=focus + i * 100,
                         video_seconds=5.0)
            for i in range(1, 8)
        ]
        engine.consider(state, shots)
        # Invariant after every pass.
        assert engine.budget.reserved_usd + engine.budget.spent_usd <= cap + 1e-6
        focus += 200
        clock.advance(50.0)
    assert engine.budget.remaining_usd() >= 0.0


def test_engine_routes_low_probability_to_cheap_model() -> None:
    """High-prob (yet renderable-in-time) shots get premium; far low-prob get cheap.

    A wide reach horizon makes the near shot both high-probability *and* far enough
    ahead that even the slow premium id can land before the reader arrives — so the
    engine reserves premium for it. The far shot is a low-probability long-shot and
    is routed to the cheap turbo id (the §4.6 reservation rule).
    """
    clock = VirtualClock()
    engine = SpeculationEngine(
        EngineConfig(
            speculative_budget_usd=20.0,
            planner=PlannerPolicy(budget_usd=20.0, min_ev_per_dollar=0.0),
            base_reach=ReachModel(linear_horizon_s=200.0),
        ),
        clock=clock,
    )
    state = ReaderState(focus_word=0, velocity_wps=4.0)
    # hi: ETA 60s, p≈0.74 → premium (50s latency fits 60s ETA).
    hi = UpcomingShot(shot_key="hi", word_start=240, video_seconds=5.0)
    # lo: ETA 400s, p≈0.14 → cheap.
    lo = UpcomingShot(shot_key="lo", word_start=1600, video_seconds=5.0)
    result = engine.consider(state, [hi, lo])
    chosen = {c.shot_key: c for c in result.launched}
    assert "hi" in chosen and "lo" in chosen
    assert chosen["hi"].model_class is ModelClass.PREMIUM
    assert chosen["lo"].model_class is ModelClass.CHEAP


def test_engine_does_not_relaunch_active_speculation() -> None:
    """A second pass over the same shots does not re-reserve already-launched ones."""
    clock = VirtualClock()
    engine = SpeculationEngine(
        EngineConfig(
            speculative_budget_usd=5.0,
            planner=PlannerPolicy(budget_usd=5.0, min_ev_per_dollar=0.0),
        ),
        clock=clock,
    )
    state = ReaderState(focus_word=0, velocity_wps=4.0)
    shots = _shots(4, start=100, step=150)
    first = engine.consider(state, shots)
    reserved_after_first = engine.budget.reserved_usd
    second = engine.consider(state, shots)  # identical state
    # No new launches (all already active) and no extra reservation.
    assert second.launched == []
    assert engine.budget.reserved_usd == pytest.approx(reserved_after_first)
    assert first.launched


def test_engine_cached_shot_is_free_hit() -> None:
    """A shot the cache already has is never re-rendered (free buffer hit)."""
    clock = VirtualClock()
    cache = SetCache(present={"already"})
    engine = SpeculationEngine(
        EngineConfig(
            speculative_budget_usd=5.0,
            planner=PlannerPolicy(budget_usd=5.0, min_ev_per_dollar=0.0),
        ),
        cache=cache,
        clock=clock,
    )
    state = ReaderState(focus_word=0, velocity_wps=4.0)
    shots = [UpcomingShot(shot_key="already", word_start=40, video_seconds=5.0)]
    result = engine.consider(state, shots)
    assert "already" not in result.launched_keys


def test_eta_helper_is_forward_only() -> None:
    """ETA is zero for a behind/at-position shot, positive ahead (sanity)."""
    assert _eta_seconds(50, 100, 4.0) == 0.0  # behind
    assert _eta_seconds(140, 100, 4.0) == pytest.approx(10.0)  # 40 words / 4 wps
