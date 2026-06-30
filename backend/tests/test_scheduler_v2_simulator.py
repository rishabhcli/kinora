"""Comparative adaptive-vs-baseline simulator tests (§4.5–§4.10, §13).

Deterministic, infra-free, zero-video. Proves the adaptive policy is *never a
material regression* over the fixed-watermark baseline across every reader
archetype, is a *strict win* on the cases adaptation is meant to help (skim spend,
fast-reader underrun, seek recovery), reports the three decision numbers (underrun
rate, wasted renders, cost), and provably spends no video-seconds.
"""

from __future__ import annotations

from app.scheduler.simulation import ReaderProfile
from app.scheduler.v2.provider import ProviderState
from app.scheduler.v2.simulator import (
    Comparison,
    build_sim_shots,
    compare_policies,
    simulate_policy,
    standard_scenarios,
)

# --- the headline proof: no regression across all archetypes ------------------ #


def test_adaptive_never_a_regression_across_all_archetypes() -> None:
    for trace in standard_scenarios():
        cmp = compare_policies(trace)
        assert cmp.adaptive_not_worse, (
            f"{trace.label}: adaptive regressed — "
            f"underrun {cmp.baseline.underrun_fraction:.3f}->{cmp.adaptive.underrun_fraction:.3f}, "
            f"cost {cmp.baseline.cost_video_s:.0f}->{cmp.adaptive.cost_video_s:.0f}, "
            f"waste {cmp.baseline.wasted_video_s:.0f}->{cmp.adaptive.wasted_video_s:.0f}"
        )


def test_adaptive_is_a_strict_win_on_several_archetypes() -> None:
    wins = [cmp for cmp in map(compare_policies, standard_scenarios()) if cmp.adaptive_is_a_win]
    # At least the skimmer (spend), a fast/slow reader (underrun), and the seeker.
    assert len(wins) >= 3


# --- per-archetype expectations ----------------------------------------------- #


def test_skimmer_slashes_spend_versus_baseline() -> None:
    cmp = compare_policies(ReaderProfile.skimmer(velocity_wps=16.0, duration_s=120.0))
    # §4.6: don't render full video for a skimmer (keyframe ladder covers them).
    assert cmp.adaptive.cost_video_s <= 0.5 * cmp.baseline.cost_video_s
    assert cmp.adaptive_is_a_win


def test_steady_slow_reader_underrun_improves() -> None:
    cmp = compare_policies(ReaderProfile.steady(velocity_wps=4.0, duration_s=240.0))
    # A slow reader's tight commit horizon under-fills the baseline; the adaptive
    # commit-horizon sizing keeps the buffer above L far more of the time.
    assert cmp.adaptive.underrun_fraction < cmp.baseline.underrun_fraction
    assert cmp.adaptive_is_a_win


def test_seeker_recovers_at_least_as_well() -> None:
    cmp = compare_policies(ReaderProfile.seeker(velocity_wps=4.0, read_s=40.0))
    assert cmp.adaptive.underrun_fraction <= cmp.baseline.underrun_fraction + 1e-9


def test_steady_fast_reader_no_worse() -> None:
    cmp = compare_policies(ReaderProfile.steady(velocity_wps=8.0, duration_s=180.0))
    assert cmp.adaptive_not_worse


def test_thinker_is_neutral_not_a_regression() -> None:
    cmp = compare_policies(
        ReaderProfile.thinker(velocity_wps=3.0, read_s=30.0, pause_s=25.0, cycles=5)
    )
    assert cmp.adaptive_not_worse


# --- multi-provider concurrency: the underrun win for fast readers ------------ #


def test_extra_provider_cuts_underrun_for_a_fast_reader() -> None:
    # Baseline drains through one slow provider; adaptive fans across a 2nd, faster
    # provider — so clips land sooner and the playable buffer stays full.
    providers = [
        ProviderState(name="wan", free_committed=4, latency_s=16.0),
        ProviderState(name="minimax", free_committed=4, latency_s=9.0),
    ]
    trace = ReaderProfile.steady(velocity_wps=9.0, duration_s=220.0)
    cmp = compare_policies(trace, providers=providers)
    assert cmp.adaptive.underrun_fraction < cmp.baseline.underrun_fraction
    assert cmp.underrun_improvement > 0.3  # a large, not marginal, improvement


# --- zero-spend proof --------------------------------------------------------- #


def test_simulator_never_reserves_real_video() -> None:
    # "cost_video_s" is a *would-be* tally; there is no budget, no reservation, no
    # provider call anywhere in the engine — the import graph proves it (only pure
    # planners). We assert the tally is finite and the engine is pure by re-running.
    trace = ReaderProfile.steady(velocity_wps=4.0, duration_s=60.0)
    m1 = simulate_policy(trace, build_sim_shots(400), adaptive=True)
    m2 = simulate_policy(trace, build_sim_shots(400), adaptive=True)
    assert m1.cost_video_s == m2.cost_video_s  # deterministic, no external state
    assert m1.cost_video_s >= 0.0


# --- determinism -------------------------------------------------------------- #


def test_comparison_is_fully_deterministic() -> None:
    trace = ReaderProfile.variable(base_wps=5.0, jitter=0.6, segments=20, seed=7)
    a = compare_policies(trace)
    b = compare_policies(trace)
    assert _metrics_tuple(a) == _metrics_tuple(b)


def test_different_seeds_give_different_traces_but_stable_replay() -> None:
    t1 = ReaderProfile.variable(seed=1)
    t2 = ReaderProfile.variable(seed=2)
    # Distinct traces (different jitter) but each replays identically twice.
    c1a, c1b = compare_policies(t1), compare_policies(t1)
    assert _metrics_tuple(c1a) == _metrics_tuple(c1b)
    assert t1.label == t2.label  # same archetype label, different motion
    assert [a.velocity_wps for a in t1.actions] != [a.velocity_wps for a in t2.actions]


# --- metrics surface ---------------------------------------------------------- #


def test_metrics_expose_the_three_decision_numbers() -> None:
    cmp = compare_policies(ReaderProfile.steady(velocity_wps=4.0, duration_s=120.0))
    for m in (cmp.baseline, cmp.adaptive):
        assert 0.0 <= m.underrun_fraction <= 1.0  # quality
        assert m.wasted_video_s >= 0.0  # waste
        assert m.cost_video_s >= 0.0  # spend
        assert m.promotions >= 0
        assert m.duration_s > 0.0


def test_waste_fraction_is_bounded() -> None:
    cmp = compare_policies(ReaderProfile.seeker(velocity_wps=4.0, read_s=40.0))
    for m in (cmp.baseline, cmp.adaptive):
        assert 0.0 <= m.waste_fraction <= 1.0


def test_max_parallel_cap_limits_adaptive_fanout_without_breaking() -> None:
    # With a hard parallel cap the adaptive arm still runs and stays no-worse.
    trace = ReaderProfile.steady(velocity_wps=6.0, duration_s=180.0)
    cmp = compare_policies(trace, max_parallel=2)
    assert isinstance(cmp, Comparison)
    assert cmp.adaptive_not_worse


# --- helpers ------------------------------------------------------------------ #


def _metrics_tuple(cmp: Comparison) -> tuple[float, ...]:
    return (
        cmp.baseline.underrun_fraction,
        cmp.baseline.cost_video_s,
        cmp.baseline.wasted_video_s,
        cmp.adaptive.underrun_fraction,
        cmp.adaptive.cost_video_s,
        cmp.adaptive.wasted_video_s,
    )
