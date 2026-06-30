"""The opt-in adaptive strategy facade (kinora.md §4.5/§4.6/§4.9).

The v2 modules — :mod:`velocity`, :mod:`provider`, :mod:`prefetch` — are pure
planners. This module composes them into a single, stateless **strategy object**
the existing :class:`~app.scheduler.service.SchedulerService` can *opt into* via
the ``scheduler_v2_enabled`` flag, without rewriting its control loop.

It deliberately mirrors the shape of the decisions the §4.9 loop already makes, so
wiring is a small additive hook, not a rewrite:

* :meth:`AdaptiveStrategy.watermarks` — returns the regime-sized ``(L, H, C)`` the
  loop should fill against this tick (falls back to the §4.5 base when disabled or
  cold-start), plus the :class:`~app.scheduler.v2.velocity.RegimeVerdict` so the
  loop / telemetry can see *why*.
* :meth:`AdaptiveStrategy.plan_fill` — given the budget-approved candidates and a
  provider/lane capacity snapshot, returns the per-tick promotion fan-out
  (:class:`~app.scheduler.v2.provider.PromotionPlan`). The loop enqueues the
  assignments under its *unchanged* ``can_render_live()`` gate.
* :meth:`AdaptiveStrategy.plan_cold_zone` — the cold-zone prefetch/eviction plan
  for the cheap keyframe lane (no video-seconds).

The strategy holds **no mutable scheduler state**: the per-reader
:class:`~app.scheduler.v2.velocity.VelocityRegimeModel` lives with the session (it
persists in Redis next to the control state), and is passed in. The strategy is a
pure function bundle with its config baked in, so it is trivially testable and
spends nothing — every promotion still flows through the caller's budget gate.

Disabling (the default) makes every method a transparent pass-through to the §4.5
constants / plain reading-order fill, so a deployment with ``scheduler_v2_enabled``
off behaves byte-for-byte like today.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.scheduler.adaptive import AdaptiveConfig, Watermarks, base_watermarks
from app.scheduler.v2.prefetch import (
    CacheEntry,
    PrefetchCandidate,
    PrefetchConfig,
    PrefetchPlan,
    plan_cold_zone,
)
from app.scheduler.v2.provider import (
    PromotionCandidate,
    PromotionPlan,
    ProviderState,
    plan_promotions,
)
from app.scheduler.v2.velocity import (
    RegimeConfig,
    RegimeVerdict,
    VelocityRegimeModel,
    size_watermarks,
)


@dataclass(frozen=True, slots=True)
class AdaptiveStrategy:
    """The composed adaptive policy — opt-in via ``scheduler_v2_enabled`` (§4.6).

    Construct once (cheaply) per process from :meth:`from_settings`; call its
    methods per scheduler tick with the session's regime model and a capacity
    snapshot. Pure: no I/O, no spend, no mutable scheduler state.
    """

    enabled: bool
    base: Watermarks
    regime_config: RegimeConfig
    adaptive_config: AdaptiveConfig
    prefetch_config: PrefetchConfig
    max_parallel: int | None
    default_provider_latency_s: float

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> AdaptiveStrategy:
        """Build the strategy from :class:`~app.core.config.Settings` (§4.5/§4.6)."""
        s = settings or get_settings()
        return cls(
            enabled=s.scheduler_v2_enabled,
            base=base_watermarks(s),
            regime_config=RegimeConfig(
                skim_ceiling_multiple=s.scheduler_v2_skim_ceiling_multiple,
                reread_backward_fraction=s.scheduler_v2_reread_backward_fraction,
                ponder_dwell_ms=s.scheduler_v2_ponder_dwell_ms,
                min_samples=s.scheduler_v2_regime_min_samples,
            ),
            adaptive_config=AdaptiveConfig(),
            prefetch_config=PrefetchConfig(
                prefetch_depth=s.scheduler_v2_prefetch_depth,
                cache_capacity=s.scheduler_v2_cold_cache_capacity,
            ),
            max_parallel=(
                s.scheduler_v2_max_parallel_promotions
                if s.scheduler_v2_max_parallel_promotions > 0
                else None
            ),
            default_provider_latency_s=s.scheduler_v2_provider_latency_s,
        )

    # -- watermark sizing ---------------------------------------------------- #

    def watermarks(self, model: VelocityRegimeModel) -> tuple[Watermarks, RegimeVerdict | None]:
        """The ``(L, H, C)`` to fill against this tick + the regime verdict (§4.5).

        When disabled, returns the §4.5 base unchanged and ``None`` (no regime
        analysis). When enabled, returns the regime-sized watermarks and the
        verdict that produced them.
        """
        if not self.enabled:
            return self.base, None
        return size_watermarks(
            self.base,
            model,
            regime_config=self.regime_config,
            adaptive_config=self.adaptive_config,
        )

    # -- promotion fan-out --------------------------------------------------- #

    def plan_fill(
        self,
        candidates: list[PromotionCandidate],
        providers: list[ProviderState],
    ) -> PromotionPlan:
        """The per-tick promotion fan-out across free provider slots (§4.9).

        When disabled, falls back to a single-provider, reading-order assignment up
        to the free committed width (the de-facto §4.9 behaviour today). When
        enabled, uses the soonest-landing provider-aware planner. Either way the
        caller enqueues the assignments under its own budget gate.
        """
        if not candidates or not providers:
            return PromotionPlan(deferred=list(candidates))
        return plan_promotions(
            candidates,
            providers,
            max_parallel=self.max_parallel if self.enabled else _committed_width(providers),
        )

    # -- cold-zone prefetch / eviction -------------------------------------- #

    def plan_cold_zone(
        self,
        cache: list[CacheEntry],
        prefetch_candidates: list[PrefetchCandidate],
        *,
        focus_word: int,
        velocity_wps: float,
    ) -> PrefetchPlan:
        """The cold-zone prefetch + eviction plan (§4.4) — no video-seconds.

        When disabled, returns an empty plan (the §4.4 cold zone does nothing, as
        today). When enabled, returns the keyframes to pre-warm + entries to evict.
        """
        if not self.enabled:
            return PrefetchPlan()
        return plan_cold_zone(
            cache,
            prefetch_candidates,
            focus_word=focus_word,
            velocity_wps=velocity_wps,
            config=self.prefetch_config,
        )


def _committed_width(providers: list[ProviderState]) -> int:
    """The total free committed slot count (the baseline fan-out cap)."""
    return max(1, sum(p.free_committed for p in providers if p.healthy))


__all__ = [
    "AdaptiveStrategy",
]
