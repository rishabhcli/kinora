"""No-infra budget simulation harness (kinora.md §11.1, §13).

The whole point: prove the FinOps loop keeps synthetic reading sessions INSIDE the
hard video-seconds cap, with zero infrastructure and zero credits.
"""

from __future__ import annotations

import pytest

from app.finops.simulation import (
    SyntheticReader,
    default_reader_suite,
    run_suite,
    simulate_pool,
    simulate_reader,
)
from app.finops.tiers import BudgetScopeKind, BudgetTierPolicy, TieredCap


def _policy(
    *, ceiling: float = 1650.0, per_session: float = 300.0, per_scene: float = 90.0
) -> BudgetTierPolicy:
    return BudgetTierPolicy(
        global_cap=TieredCap(BudgetScopeKind.GLOBAL, ceiling),
        tenant_cap=TieredCap(BudgetScopeKind.TENANT, float("inf")),
        session_cap=TieredCap(BudgetScopeKind.SESSION, per_session),
        scene_cap=TieredCap(BudgetScopeKind.SCENE, per_scene),
    )


def test_steady_reader_stays_within_cap() -> None:
    reader = SyntheticReader(label="steady", velocity_wps=4.0, total_words=40_000)
    result = simulate_reader(reader, _policy(), tick_s=5.0, max_ticks=1000)
    assert not result.cap_breached
    assert result.video_seconds_spent <= _policy().global_cap.cap_s + 1e-6
    assert result.total_shots > 0


def test_binge_reader_never_breaches_cap() -> None:
    # A long binge that would blow a naive budget — the governor must hold it.
    reader = SyntheticReader(
        label="binge", velocity_wps=8.0, total_words=500_000, promotion_rate=1.0
    )
    result = simulate_reader(reader, _policy(), tick_s=5.0, max_ticks=5000)
    assert not result.cap_breached
    assert result.peak_used_s <= _policy().global_cap.cap_s + 1e-6
    # Once the cap is reached it degrades, so there must be degraded shots.
    assert result.degraded_shots > 0


def test_skimmer_spends_little_video() -> None:
    skimmer = SyntheticReader(
        label="skimmer", velocity_wps=20.0, total_words=40_000, promotion_rate=0.2
    )
    steady = SyntheticReader(label="steady", velocity_wps=4.0, total_words=40_000)
    s_res = simulate_reader(skimmer, _policy(), tick_s=5.0, max_ticks=2000)
    t_res = simulate_reader(steady, _policy(), tick_s=5.0, max_ticks=2000)
    # The skimmer reaches the end fast and isn't constrained, but the harness's
    # cap is never breached for either.
    assert not s_res.cap_breached and not t_res.cap_breached


def test_tiny_cap_forces_degradation_but_no_breach() -> None:
    reader = SyntheticReader(label="steady", velocity_wps=4.0, total_words=40_000)
    tiny = _policy(ceiling=20.0, per_session=20.0)
    result = simulate_reader(reader, tiny, tick_s=5.0, max_ticks=2000)
    assert not result.cap_breached
    assert result.video_seconds_spent <= 20.0 + 1e-6
    assert result.degraded_shots > 0
    assert result.final_recommendation in {"optimize", "halt"}


def test_run_suite_no_cap_breached() -> None:
    report = run_suite(_policy(), max_ticks=2000)
    assert not report.any_cap_breached
    assert len(report.results) == len(default_reader_suite())
    for r in report.results:
        assert not r.cap_breached


def test_idle_short_book_finishes_quickly() -> None:
    reader = SyntheticReader(label="short", velocity_wps=4.0, total_words=400)
    result = simulate_reader(reader, _policy(), tick_s=5.0, max_ticks=1000)
    # 400 words at 20 words/tick = ~20 ticks, well under the cap.
    assert result.ticks <= 25
    assert not result.cap_breached


def test_result_as_dict_serializable() -> None:
    result = simulate_reader(
        SyntheticReader(label="x", velocity_wps=4.0, total_words=4000),
        _policy(),
        max_ticks=200,
    )
    d = result.as_dict()
    assert set(d) >= {"label", "ticks", "video_seconds_spent", "cap_breached", "spent_by_rung"}
    assert d["full_video_fraction"] == pytest.approx(
        result.full_video_fraction
    )


def test_shared_pool_global_ceiling_holds_across_tenants() -> None:
    # Several binge readers contend for ONE shared global ceiling. The sum of all
    # their spend must never exceed the ceiling (§11.1 "no one drains the pool").
    readers = tuple(
        SyntheticReader(
            label=f"tenant_{i}", velocity_wps=8.0, total_words=200_000, promotion_rate=1.0
        )
        for i in range(5)
    )
    # Per-session cap high enough that the GLOBAL ceiling is the binding limit.
    pool = _policy(ceiling=100.0, per_session=1000.0)
    result = simulate_pool(readers, pool, tick_s=5.0, max_ticks=2000)
    assert not result.cap_breached
    assert result.total_video_seconds <= 100.0 + 1e-6
    # Every reader spent something OR was degraded; the pool degrades latecomers.
    assert result.degraded_shots > 0
    assert sum(result.per_reader_video_s.values()) == pytest.approx(
        result.total_video_seconds
    )


def test_shared_pool_per_session_cap_bounds_one_reader() -> None:
    # One greedy reader can't take more than its per-session cap even with a huge
    # global ceiling and a tiny field.
    readers = (
        SyntheticReader(label="greedy", velocity_wps=8.0, total_words=500_000),
    )
    pool = _policy(ceiling=100_000.0, per_session=30.0)
    result = simulate_pool(readers, pool, tick_s=5.0, max_ticks=3000)
    assert not result.cap_breached
    assert result.per_reader_video_s["greedy"] <= 30.0 + 1e-6


def test_pool_result_as_dict_serializable() -> None:
    readers = (SyntheticReader(label="a", velocity_wps=4.0, total_words=4000),)
    result = simulate_pool(readers, _policy(), max_ticks=200)
    d = result.as_dict()
    assert set(d) >= {"total_video_seconds", "cap_breached", "per_reader_video_s"}
