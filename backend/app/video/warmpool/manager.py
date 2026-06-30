"""Multi-provider warm-pool manager + keep-alive / pre-warm scheduler.

The manager owns one :class:`~app.video.warmpool.pool.ProviderPool` per provider
and the per-provider :class:`~app.video.warmpool.demand.DemandModel`. It runs the
keep-alive loop that, every ``keepalive_interval_s``, turns predicted near-term
demand into a warm target and asks each pool to maintain itself toward it.

This is the cost-aware control loop:

1. The render path / scheduler feeds demand: :meth:`record_dispatch` (a render
   just started) and :meth:`hint` (the scheduler's look-ahead, driven by reader
   velocity / buffer watermark — the §5.3 seam).
2. Each keep-alive tick recomputes ``warm_target = demand.warm_target(...)`` from
   the effective demand rate, the prewarm horizon, the pool bounds, and whether
   the provider's measured cold start is even worth hiding.
3. The pool maintains itself: drain-if-unhealthy, evict idle, recycle stale, top
   up to target. The demand rate decays on idle ticks so the target relaxes back
   to the floor after a burst clears.

The loop is a single ``asyncio.Task`` whose only blocking call is
``clock.sleep`` — under a :class:`~app.video.warmpool.clock.VirtualClock` a test
drives it tick-by-tick with ``await clock.advance(...)``. The manager never
renders and never reads ``KINORA_LIVE_VIDEO``.
"""

from __future__ import annotations

import asyncio
import contextlib

from app.core.logging import get_logger

from .clock import SYSTEM_CLOCK, Clock
from .cost import ColdStartModel
from .demand import DemandModel
from .lease import LeaseError
from .pool import PoolStats, ProviderPool
from .protocols import HealthSignal, ProviderId, SessionFactory
from .settings import WarmPoolConfig

logger = get_logger("app.video.warmpool.manager")


class WarmPoolManager:
    """Owns the per-provider pools + the keep-alive scheduler (the public API)."""

    def __init__(
        self,
        factory: SessionFactory,
        *,
        config: WarmPoolConfig | None = None,
        clock: Clock | None = None,
        health: dict[ProviderId, HealthSignal] | None = None,
    ) -> None:
        self._factory = factory
        self._config = config or WarmPoolConfig()
        self._clock = clock or SYSTEM_CLOCK
        self._health = health or {}
        self._pools: dict[ProviderId, ProviderPool] = {}
        self._demand: dict[ProviderId, DemandModel] = {}
        # renders dispatched since the last tick, per provider (for the rate window).
        self._dispatch_window: dict[ProviderId, int] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    # ------------------------------------------------------------------ #
    # provider registration
    # ------------------------------------------------------------------ #

    def register(self, provider: ProviderId, *, health: HealthSignal | None = None) -> ProviderPool:
        """Register (or fetch) the pool for ``provider``. Idempotent."""
        if provider in self._pools:
            return self._pools[provider]
        cost = ColdStartModel(provider=provider)
        pool = ProviderPool(
            provider,
            self._factory,
            clock=self._clock,
            config=self._config,
            cost=cost,
            health=health or self._health.get(provider),
        )
        self._pools[provider] = pool
        self._demand[provider] = DemandModel(provider=provider)
        self._dispatch_window[provider] = 0
        return pool

    def pool(self, provider: ProviderId) -> ProviderPool:
        """Get the pool for ``provider`` (registering on first use)."""
        return self._pools.get(provider) or self.register(provider)

    def demand(self, provider: ProviderId) -> DemandModel:
        """Get the demand model for ``provider`` (registering on first use)."""
        if provider not in self._demand:
            self.register(provider)
        return self._demand[provider]

    @property
    def providers(self) -> tuple[ProviderId, ...]:
        return tuple(self._pools)

    # ------------------------------------------------------------------ #
    # demand inputs (the seams the render path / scheduler drive)
    # ------------------------------------------------------------------ #

    def record_dispatch(self, provider: ProviderId, count: int = 1) -> None:
        """Note that ``count`` renders were just dispatched for ``provider``.

        Accumulated into a per-tick window; folded into the demand EWMA on the next
        keep-alive tick. Drives the *reactive* half of the warm target.
        """
        self.demand(provider)  # ensure registered
        self._dispatch_window[provider] = self._dispatch_window.get(provider, 0) + max(0, count)

    def hint(self, provider: ProviderId, renders_per_s: float) -> None:
        """Set the scheduler's look-ahead demand rate (the *predictive* seam).

        This is reader-velocity / buffer-watermark pressure entering the pool: the
        scheduler knows it is about to need shots and warms sessions *ahead* of the
        first cold render.
        """
        self.demand(provider).set_hint(renders_per_s)

    # ------------------------------------------------------------------ #
    # borrow passthrough (so callers hold the manager, not each pool)
    # ------------------------------------------------------------------ #

    def borrow(self, provider: ProviderId, *, timeout_s: float | None = None):  # type: ignore[no-untyped-def]
        """Borrow a warm session for ``provider`` (async context manager)."""
        return self.pool(provider).borrow(timeout_s=timeout_s)

    # ------------------------------------------------------------------ #
    # the keep-alive scheduler
    # ------------------------------------------------------------------ #

    def _recompute_target(self, provider: ProviderId) -> int:
        """Demand + cost → warm target for one provider (pure given current state)."""
        pool = self._pools[provider]
        demand = self._demand[provider]
        cfg = self._config
        # fold the dispatch window into the demand rate, then clear it.
        window = self._dispatch_window.get(provider, 0)
        demand.observe(window, window_s=cfg.keepalive_interval_s)
        if window == 0:
            demand.decay_idle()
        self._dispatch_window[provider] = 0
        worth = pool.cost.worth_warming(threshold_s=cfg.warm_worth_threshold_s)
        target = demand.warm_target(
            horizon_s=cfg.prewarm_horizon_s,
            min_warm=cfg.min_warm,
            max_warm=cfg.max_warm,
            worth_warming=worth,
        )
        pool.warm_target = target
        return target

    async def tick(self) -> None:
        """Run one keep-alive pass over every registered pool (recompute + maintain).

        Exposed so tests can drive the loop deterministically without the timer.
        """
        for provider in list(self._pools):
            self._recompute_target(provider)
            try:
                await self._pools[provider].maintain()
            except LeaseError:
                # draining races are expected; the next tick reconciles.
                logger.debug("warmpool.tick.lease_race", provider=provider)
            except Exception:  # never let one provider kill the loop
                logger.warning("warmpool.tick.maintain_failed", provider=provider)

    async def _run_loop(self) -> None:
        while not self._stopped.is_set():
            await self.tick()
            await self._clock.sleep(self._config.keepalive_interval_s)

    async def start(self) -> None:
        """Start the keep-alive loop (no-op when disabled or already running)."""
        if not self._config.enabled or self._loop_task is not None:
            return
        self._stopped.clear()
        self._loop_task = asyncio.ensure_future(self._run_loop())

    async def stop(self) -> None:
        """Stop the keep-alive loop and close every pool (releases all sessions)."""
        self._stopped.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        for pool in self._pools.values():
            await pool.aclose()

    # ------------------------------------------------------------------ #
    # telemetry
    # ------------------------------------------------------------------ #

    def stats(self) -> dict[ProviderId, PoolStats]:
        """A snapshot of every pool (for the §13 metrics panel / leak assertions)."""
        return {p: pool.stats() for p, pool in self._pools.items()}


__all__ = ["WarmPoolManager"]
