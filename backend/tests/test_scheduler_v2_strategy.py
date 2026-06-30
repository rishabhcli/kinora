"""Opt-in adaptive strategy facade tests (§4.5/§4.6/§4.9).

Pure, no infra. Pins :class:`app.scheduler.v2.strategy.AdaptiveStrategy`: disabled
(the default) it is a transparent pass-through to the §4.5 base / plain reading
order; enabled it routes through the regime sizer, the provider-aware planner, and
the cold-zone policy. Construction reads the v2 settings.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.scheduler.adaptive import base_watermarks
from app.scheduler.v2.prefetch import CacheEntry, PrefetchCandidate
from app.scheduler.v2.provider import Lane, PromotionCandidate, ProviderState
from app.scheduler.v2.strategy import AdaptiveStrategy
from app.scheduler.v2.velocity import ReaderRegime, VelocityRegimeModel


def _enabled_settings() -> Settings:
    base = get_settings()
    return base.model_copy(update={"scheduler_v2_enabled": True})


def _disabled_settings() -> Settings:
    base = get_settings()
    return base.model_copy(update={"scheduler_v2_enabled": False})


def _steady_model(wps: float, n: int = 12) -> VelocityRegimeModel:
    m = VelocityRegimeModel.fresh(velocity_wps=wps)
    for _ in range(n):
        m.observe(words_advanced=int(round(wps)), dt_ms=1000.0)
    return m


# --- disabled = transparent pass-through ------------------------------------- #


def test_disabled_watermarks_return_base_unchanged() -> None:
    s = _disabled_settings()
    strat = AdaptiveStrategy.from_settings(s)
    assert not strat.enabled
    wm, verdict = strat.watermarks(_steady_model(8.0))
    assert wm.as_tuple() == base_watermarks(s).as_tuple()
    assert verdict is None  # no regime analysis when disabled


def test_disabled_cold_zone_is_empty() -> None:
    strat = AdaptiveStrategy.from_settings(_disabled_settings())
    plan = strat.plan_cold_zone(
        [], [PrefetchCandidate(key="k", word_index_start=100)],
        focus_word=0, velocity_wps=4.0,
    )
    assert plan.prefetch == []
    assert plan.evict == []


def test_disabled_fill_uses_reading_order_up_to_free_width() -> None:
    strat = AdaptiveStrategy.from_settings(_disabled_settings())
    cands = [
        PromotionCandidate(shot_id=f"s{i}", est_duration_s=5.0, eta_s=float(i * 5))
        for i in range(6)
    ]
    providers = [ProviderState(name="wan", free_committed=4, latency_s=12.0)]
    plan = strat.plan_fill(cands, providers)
    # Disabled falls back to the committed-width fan-out (4 slots).
    assert plan.promoted == 4


# --- enabled = the adaptive policy ------------------------------------------- #


def test_enabled_sizes_watermarks_by_regime() -> None:
    strat = AdaptiveStrategy.from_settings(_enabled_settings())
    assert strat.enabled
    # A skimmer collapses the band toward base; verdict is surfaced.
    wm, verdict = strat.watermarks(_steady_model(16.0))
    assert verdict is not None
    assert verdict.regime is ReaderRegime.SKIMMING


def test_enabled_fill_fans_across_providers() -> None:
    strat = AdaptiveStrategy.from_settings(_enabled_settings())
    cands = [
        PromotionCandidate(shot_id=f"s{i}", est_duration_s=5.0, eta_s=float(i * 5))
        for i in range(4)
    ]
    providers = [
        ProviderState(name="a", free_committed=2, latency_s=10.0),
        ProviderState(name="b", free_committed=2, latency_s=10.0),
    ]
    plan = strat.plan_fill(cands, providers)
    assert plan.promoted == 4
    assert {a.provider for a in plan.assignments} == {"a", "b"}
    assert all(a.lane is Lane.COMMITTED for a in plan.assignments)


def test_enabled_cold_zone_prefetches_and_evicts() -> None:
    s = _enabled_settings().model_copy(
        update={"scheduler_v2_prefetch_depth": 1, "scheduler_v2_cold_cache_capacity": 1}
    )
    strat = AdaptiveStrategy.from_settings(s)
    cache = [CacheEntry(key="c100", word_index_start=100, inserted_seq=0)]
    plan = strat.plan_cold_zone(
        cache, [PrefetchCandidate(key="k200", word_index_start=200)],
        focus_word=0, velocity_wps=4.0,
    )
    assert plan.prefetch == ["k200"]
    assert plan.evict == ["c100"]


def test_enabled_respects_max_parallel_setting() -> None:
    s = _enabled_settings().model_copy(update={"scheduler_v2_max_parallel_promotions": 2})
    strat = AdaptiveStrategy.from_settings(s)
    assert strat.max_parallel == 2
    cands = [
        PromotionCandidate(shot_id=f"s{i}", est_duration_s=5.0, eta_s=float(i * 5))
        for i in range(6)
    ]
    providers = [ProviderState(name="wan", free_committed=6, latency_s=10.0)]
    plan = strat.plan_fill(cands, providers)
    assert plan.promoted == 2  # the hard cap


# --- empty / edge inputs ------------------------------------------------------ #


def test_fill_with_no_candidates_or_providers_is_safe() -> None:
    strat = AdaptiveStrategy.from_settings(_enabled_settings())
    assert strat.plan_fill([], [ProviderState(name="p", free_committed=4)]).promoted == 0
    cands = [PromotionCandidate(shot_id="s", est_duration_s=5.0, eta_s=5.0)]
    assert strat.plan_fill(cands, []).promoted == 0
