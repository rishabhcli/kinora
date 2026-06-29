"""Property tests for the zero-IO §9.7 render control-flow simulator.

The simulator reuses the *production* decision modules (``decide_qa``,
``decide_retry``, ``plan_ladder``, the ``ShotStateMachine``, the ``PoisonTracker``)
so a property proven over generated scenarios is a property of the live loop's
control flow. These assert the engine's hard guarantees over the whole scenario
space:

* **termination + sink** — every shot ends in a terminal §9.7 state;
* **retry cap (§9.5)** — attempts never exceed ``RetryPolicy.max_attempts``;
* **legal walk** — the recorded state path is a legal §9.7 edge sequence;
* **budget honesty** — video-seconds are zero on every non-live path and bounded
  by ``attempts × duration`` on the live path (and exactly zero when the live gate
  is shut), so the off-gate zero-spend invariant holds mechanically;
* **degrade ⇒ a feasible rung** — a degraded shot always lands on a rung the
  ladder says is feasible.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from app.render.ladder import LadderAssets, LadderReason, Rung, cost_class, plan_ladder, rank
from app.render.retry import RetryPolicy
from app.render.simulator import (
    QAVerdict,
    RenderScenario,
    RenderSimulator,
    simulate,
)
from app.render.states import RenderState, is_allowed
from app.verification.properties.strategies import ladder_assets, render_scenarios

TERMINAL = {RenderState.ACCEPTED, RenderState.DEGRADED, RenderState.CONFLICT}


@given(render_scenarios())
def test_always_reaches_a_terminal_state(scenario: RenderScenario) -> None:
    """Every scenario drives the shot to a sink (accepted / degraded / surfaced)."""
    report = simulate(scenario)
    assert report.final_state in TERMINAL


@given(render_scenarios())
def test_attempts_respect_the_retry_cap(scenario: RenderScenario) -> None:
    """The §9.5 retry cap: total attempts never exceed ``cap + 1``."""
    policy = RetryPolicy()
    report = RenderSimulator(policy=policy).run(scenario)
    assert report.attempts <= policy.max_attempts


@given(render_scenarios())
def test_state_path_is_a_legal_9_7_walk(scenario: RenderScenario) -> None:
    """The recorded §12.5 state trace is a legal §9.7 edge sequence (no jumps)."""
    report = simulate(scenario)
    path = report.state_path()
    for src, dst in zip(path, path[1:], strict=False):
        if src == dst:
            continue  # a recorded re-entry (e.g. RENDERING on a fresh attempt)
        assert is_allowed(src, dst), f"illegal recorded edge {src} -> {dst}"


@given(render_scenarios())
def test_video_seconds_are_nonnegative_and_bounded(scenario: RenderScenario) -> None:
    """Budget honesty: spend ≥ 0 and ≤ attempts × duration (each attempt charges once)."""
    policy = RetryPolicy()
    report = RenderSimulator(policy=policy).run(scenario)
    assert report.video_seconds >= 0.0
    ceiling = policy.max_attempts * scenario.target_duration_s + 1e-6
    assert report.video_seconds <= ceiling


@given(render_scenarios())
def test_non_live_path_spends_zero_video_seconds(scenario: RenderScenario) -> None:
    """The off-gate / low-budget / poisoned paths spend exactly zero (§ critical gotcha).

    This is the mechanical form of "KINORA_LIVE_VIDEO off ⇒ no spend": a shot the
    gate forbids degrades before any render charges the budget.
    """
    if (not scenario.live_feasible) or scenario.budget_low or scenario.already_poisoned:
        report = simulate(scenario)
        assert report.video_seconds == 0.0
        assert report.degraded or report.final_state is RenderState.DEGRADED


@given(render_scenarios())
def test_degraded_lands_on_a_feasible_rung(scenario: RenderScenario) -> None:
    """A degraded shot's rung is one the ladder says is feasible given the assets."""
    report = simulate(scenario)
    if not report.degraded:
        return
    # The audio card is always feasible (the floor), so a degrade can never be FULL_WAN.
    assert report.rung is not Rung.FULL_WAN
    feasible = {
        lane.rung
        for lane in plan_ladder(scenario.assets, LadderReason.RETRIES_EXHAUSTED).lanes
        if lane.feasible
    }
    # A poisoned shot is forced to the bottom rung regardless of richer assets.
    assert report.rung in feasible or report.rung is Rung.AUDIO_TEXT_ONLY


@given(render_scenarios())
def test_accepted_only_via_live_full_wan(scenario: RenderScenario) -> None:
    """An accepted shot shipped full video (the simulator's only accept path is live)."""
    report = simulate(scenario)
    if report.accepted:
        assert report.rung is Rung.FULL_WAN
        assert report.video_seconds > 0.0


def test_all_passing_first_attempt_accepts() -> None:
    """A clean live render with a passing first verdict accepts in one attempt."""
    scenario = RenderScenario(
        live_feasible=True,
        budget_low=False,
        assets=LadderAssets(live_feasible=True, has_keyframe=True),
        qa_sequence=[QAVerdict.passing()],
    )
    report = simulate(scenario)
    assert report.accepted
    assert report.attempts == 1
    assert report.video_seconds == scenario.target_duration_s


# --------------------------------------------------------------------------- #
# Determinism — the same scenario simulates identically every time
# --------------------------------------------------------------------------- #


@given(render_scenarios())
def test_simulation_is_deterministic(scenario: RenderScenario) -> None:
    a = simulate(scenario)
    b = simulate(scenario)
    assert a.final_state == b.final_state
    assert a.attempts == b.attempts
    assert a.video_seconds == b.video_seconds
    assert a.rung == b.rung


# --------------------------------------------------------------------------- #
# plan_ladder structural properties (the rung-selection brain)
# --------------------------------------------------------------------------- #


@given(ladder_assets, st.sampled_from(list(LadderReason)))
def test_plan_ladder_chain_is_descending(
    assets: LadderAssets, reason: LadderReason
) -> None:
    """The fallback chain is strictly descending in rank (richest → cheapest)."""
    plan = plan_ladder(assets, reason)
    ranks = [rank(r) for r in plan.chain]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)  # no rung repeats


@given(ladder_assets, st.sampled_from(list(LadderReason)))
def test_plan_ladder_cost_is_monotone_down_the_chain(
    assets: LadderAssets, reason: LadderReason
) -> None:
    """Cost class falls monotonically as the chain steps down (a §12.4 promise)."""
    plan = plan_ladder(assets, reason)
    costs = [int(cost_class(r)) for r in plan.chain]
    assert costs == sorted(costs, reverse=True)


@given(ladder_assets, st.sampled_from(list(LadderReason)))
def test_selected_is_highest_feasible_and_chain_starts_there(
    assets: LadderAssets, reason: LadderReason
) -> None:
    """``selected`` is the top feasible lane, and ``chain`` begins with it."""
    plan = plan_ladder(assets, reason)
    feasible = [lane.rung for lane in plan.lanes if lane.feasible]
    assert plan.selected == feasible[0]
    assert plan.chain[0] == plan.selected
    # The audio card is the unconditional floor — always feasible.
    assert Rung.AUDIO_TEXT_ONLY in feasible


@given(ladder_assets)
def test_only_live_ok_keeps_full_wan(assets: LadderAssets) -> None:
    """Any non-``LIVE_OK`` reason forbids the live lane even when assets allow it."""
    for reason in LadderReason:
        plan = plan_ladder(assets, reason)
        if plan.selected is Rung.FULL_WAN:
            assert reason is LadderReason.LIVE_OK
            assert assets.live_feasible
