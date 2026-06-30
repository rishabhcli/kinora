"""Deterministic unit tests for the video warm-pool / cold-start subsystem.

No infra, no network, no real video, no spend. Every timer runs on a
:class:`~app.video.warmpool.clock.VirtualClock` and every session comes from a
fake factory, so cold-start cost, idle eviction, predictive pre-warm, fair
borrow/return + exhaustion timeout, unhealthy-provider drain, and the
"no warm-session leak" invariant are all exercised without sleeping.

Covers the required scenarios:
* pool min-warm maintenance
* idle eviction (surplus, TTL-aware, floor-respecting)
* predictive pre-warm on a demand spike (scheduler hint seam)
* cold-vs-warm latency accounting drives the warm target
* borrow/return + exhaustion timeout
* unhealthy-provider drain (circuit-aware)
* no warm-session leak (every session warm | leased | closed)
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from app.video.warmpool.clock import VirtualClock
from app.video.warmpool.cost import (
    DEFAULT_SEED_COLD_S,
    ColdStartModel,
    LatencyStats,
)
from app.video.warmpool.demand import DemandModel
from app.video.warmpool.lease import LeaseTimeout, PoolDraining
from app.video.warmpool.manager import WarmPoolManager
from app.video.warmpool.pool import ProviderPool
from app.video.warmpool.settings import WarmPoolConfig

# Async tests run under pytest-asyncio's ``asyncio_mode = "auto"`` (pyproject) — no
# per-test marker needed. Every coroutine here is fully deterministic over the
# injected VirtualClock + fake factory; no real sleeping, no network, no spend.


# --------------------------------------------------------------------------- #
# Fakes (no I/O; cold-start latency simulated by advancing the virtual clock)
# --------------------------------------------------------------------------- #


class FakeSession:
    """A scriptable provider session backed by the virtual clock."""

    _ids = itertools.count(1)

    def __init__(self, provider: str, *, healthy: bool = True) -> None:
        self.provider = provider
        self.session_id = f"{provider}-{next(self._ids)}"
        self._healthy = healthy
        self.closed = False
        self.health_calls = 0

    @property
    def handle(self) -> str:
        return f"handle::{self.session_id}"

    async def healthy(self) -> bool:
        self.health_calls += 1
        return self._healthy and not self.closed

    async def close(self) -> None:
        self.closed = True


class FakeFactory:
    """Opens :class:`FakeSession`s, charging a fixed cold-start cost on the clock.

    ``open_cost_s`` is added to the virtual clock when a session is opened so the
    pool's cold-start latency measurement is non-zero and deterministic. Tracks
    every session it ever opened so a test can assert none leaked.
    """

    def __init__(
        self,
        clock: VirtualClock,
        *,
        open_cost_s: float = 2.0,
        healthy: bool = True,
        fail_opens: int = 0,
    ) -> None:
        self._clock = clock
        self._open_cost_s = open_cost_s
        self._healthy = healthy
        self._fail_opens = fail_opens
        self.opened: list[FakeSession] = []
        self.open_calls = 0

    async def open(self, provider: str) -> FakeSession:
        self.open_calls += 1
        if self._fail_opens > 0:
            self._fail_opens -= 1
            raise RuntimeError("simulated auth handshake failure")
        # Simulate the cold-start wall time by advancing the virtual clock.
        if self._open_cost_s > 0:
            await self._clock.set(self._clock.time() + self._open_cost_s)
        session = FakeSession(provider, healthy=self._healthy)
        self.opened.append(session)
        return session

    @property
    def live_sessions(self) -> list[FakeSession]:
        return [s for s in self.opened if not s.closed]


class FakeHealth:
    """A flippable circuit-health signal (the drain seam)."""

    def __init__(self, available: bool = True) -> None:
        self._available = available

    def set(self, available: bool) -> None:
        self._available = available

    def available(self) -> bool:
        return self._available


def _cfg(**kw: object) -> WarmPoolConfig:
    base: dict[str, object] = {
        "enabled": True,
        "min_warm": 1,
        "max_size": 4,
        "max_warm": 3,
        "idle_ttl_s": 120.0,
        "health_check_interval_s": 30.0,
        "max_session_age_s": 600.0,
        "keepalive_interval_s": 5.0,
        "prewarm_horizon_s": 8.0,
        "warm_worth_threshold_s": 0.5,
        "borrow_timeout_s": 10.0,
    }
    base.update(kw)
    return WarmPoolConfig(**base)  # type: ignore[arg-type]


async def _settle() -> None:
    """Yield enough times for woken coroutines to run their bodies (deterministic).

    Resolving a waiter future (a hand-off) or starting the keep-alive loop wakes a
    coroutine that then needs several more scheduling hops to run its body / open a
    session / register its next timer. A single ``await asyncio.sleep(0)`` only
    advances one hop, so tests settle the loop with a small bounded yield instead of
    guessing the exact hop count. Bounded, so it never busy-loops.
    """
    for _ in range(16):
        await asyncio.sleep(0)


def _assert_no_leak(factory: FakeFactory, pool: ProviderPool) -> None:
    """Every opened session is accounted for: warm, leased, or closed."""
    stats = pool.stats()
    live = len(factory.live_sessions)
    assert live == stats.warm + stats.leased, (
        f"leak: {live} live sessions but warm={stats.warm} leased={stats.leased}"
    )


# --------------------------------------------------------------------------- #
# cost model — pure
# --------------------------------------------------------------------------- #


def test_latency_stats_ewma_and_max() -> None:
    s = LatencyStats().observe(2.0).observe(4.0)
    assert s.samples == 2
    assert s.max_s == 4.0
    # EWMA mean is between the two with alpha weighting (not a plain average of 3).
    assert 2.0 < s.mean_s < 4.0


def test_cold_start_model_savings_and_worth() -> None:
    m = ColdStartModel(provider="p")
    # seed: cold ~2s, warm ~0.05s → big savings, worth warming.
    assert m.savings_s > 0.5
    assert m.worth_warming(threshold_s=0.5)
    # A provider that opens as fast as it reuses is not worth warming.
    fast = ColdStartModel(provider="fast")
    fast.cold = LatencyStats(mean_s=0.1, max_s=0.1, samples=5)
    fast.warm = LatencyStats(mean_s=0.05, max_s=0.05, samples=5)
    assert not fast.worth_warming(threshold_s=0.5)


def test_planning_cold_is_conservative() -> None:
    m = ColdStartModel(provider="p")
    m.cold = LatencyStats(mean_s=2.0, max_s=6.0, samples=10)
    # planning = mean + (max-mean)*headroom(0.5) = 2 + 4*0.5 = 4.0 > mean.
    assert m.planning_cold_s == pytest.approx(4.0)


# --------------------------------------------------------------------------- #
# demand model — pure
# --------------------------------------------------------------------------- #


def test_demand_target_floor_and_clamp() -> None:
    d = DemandModel(provider="p")
    # no demand, worth warming → floor = min_warm.
    assert d.warm_target(horizon_s=8.0, min_warm=1, max_warm=3, worth_warming=True) == 1
    # not worth warming → floor collapses to 0.
    assert d.warm_target(horizon_s=8.0, min_warm=1, max_warm=3, worth_warming=False) == 0


def test_demand_hint_drives_target_and_clamps_to_max() -> None:
    d = DemandModel(provider="p")
    d.set_hint(1.0)  # 1 render/s over an 8s horizon → 8, clamped to max_warm=3.
    assert d.warm_target(horizon_s=8.0, min_warm=1, max_warm=3, worth_warming=True) == 3
    # effective rate is max(observed, hint); a stale hint still lifts the target.
    assert d.effective_rate_per_s == 1.0


def test_demand_decay_relaxes_toward_floor() -> None:
    d = DemandModel(provider="p")
    d.observe(10, window_s=5.0)  # spike
    hot = d.rate_per_s
    d.decay_idle()
    assert d.rate_per_s < hot


# --------------------------------------------------------------------------- #
# clock — virtual timers fire in order, no leaks
# --------------------------------------------------------------------------- #


async def test_virtual_clock_sleep_fires_on_advance() -> None:
    clock = VirtualClock()
    fired: list[str] = []

    async def waiter(name: str, delay: float) -> None:
        await clock.sleep(delay)
        fired.append(name)

    t1 = asyncio.ensure_future(waiter("short", 1.0))
    t2 = asyncio.ensure_future(waiter("long", 3.0))
    await asyncio.sleep(0)  # let them park
    assert clock.pending_timers == 2
    await clock.advance(1.0)
    assert fired == ["short"]
    await clock.advance(2.0)
    assert fired == ["short", "long"]
    await asyncio.gather(t1, t2)
    assert clock.pending_timers == 0


# --------------------------------------------------------------------------- #
# pool: min-warm maintenance
# --------------------------------------------------------------------------- #


async def test_min_warm_maintenance_opens_floor() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock)
    pool = ProviderPool("dashscope", factory, clock=clock, config=_cfg(min_warm=2))
    pool.warm_target = 2
    await pool.maintain()
    st = pool.stats()
    assert st.warm == 2
    assert st.opens == 2
    _assert_no_leak(factory, pool)


# --------------------------------------------------------------------------- #
# pool: idle eviction
# --------------------------------------------------------------------------- #


async def test_idle_eviction_reclaims_surplus_after_ttl() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock)
    pool = ProviderPool(
        "dashscope",
        factory,
        clock=clock,
        config=_cfg(min_warm=1, idle_ttl_s=100.0, health_check_interval_s=1000.0),
    )
    # Warm up 3 sessions at a high target.
    pool.warm_target = 3
    await pool.maintain()
    assert pool.stats().warm == 3
    # Demand drops: target back to floor 1. Sessions are still fresh → not yet evicted.
    pool.warm_target = 1
    await pool.maintain()
    assert pool.stats().warm == 3  # not idle long enough yet
    # Advance past the idle TTL, then maintain: surplus (above floor) evicted to 1.
    await clock.advance(150.0)
    await pool.maintain()
    st = pool.stats()
    assert st.warm == 1
    assert st.idle_evictions == 2
    _assert_no_leak(factory, pool)


# --------------------------------------------------------------------------- #
# pool: health-checked recycling
# --------------------------------------------------------------------------- #


async def test_unhealthy_warm_session_recycled_on_probe() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock)
    pool = ProviderPool(
        "dashscope",
        factory,
        clock=clock,
        config=_cfg(min_warm=1, health_check_interval_s=10.0),
    )
    pool.warm_target = 1
    await pool.maintain()
    [sess] = factory.live_sessions
    # Make the warm session unhealthy; advance past the probe interval so maintain probes it.
    sess._healthy = False
    await clock.advance(20.0)
    await pool.maintain()
    st = pool.stats()
    assert st.health_recycles >= 1
    assert sess.closed  # the dead one was recycled
    assert st.warm == 1  # replaced by a fresh, healthy session
    _assert_no_leak(factory, pool)


async def test_session_recycled_past_max_age() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock)
    pool = ProviderPool(
        "dashscope",
        factory,
        clock=clock,
        config=_cfg(min_warm=1, max_session_age_s=50.0, health_check_interval_s=1000.0),
    )
    pool.warm_target = 1
    await pool.maintain()
    [old] = factory.live_sessions
    await clock.advance(60.0)  # past max age
    await pool.maintain()
    assert old.closed
    assert pool.stats().warm == 1
    _assert_no_leak(factory, pool)


# --------------------------------------------------------------------------- #
# pool: borrow / return + cold-vs-warm accounting
# --------------------------------------------------------------------------- #


async def test_borrow_warm_is_fast_borrow_cold_is_measured() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock, open_cost_s=2.0)
    pool = ProviderPool("dashscope", factory, clock=clock, config=_cfg(min_warm=1))
    pool.warm_target = 1
    await pool.maintain()  # one warm session, cost model has a cold sample

    # First borrow reuses the warm session: no new open.
    opens_before = factory.open_calls
    async with pool.borrow() as lease:
        assert str(lease.handle).startswith("handle::")
        assert pool.stats().leased == 1
    assert factory.open_calls == opens_before  # warm reuse, no cold open
    assert pool.stats().leased == 0
    assert pool.stats().warm == 1  # returned to the shelf

    # The cost model learned a real cold-start cost from the warm-up open.
    assert pool.cost.cold.samples >= 1
    assert pool.cost.planning_cold_s >= 1.0
    assert pool.cost.savings_s > 0.0
    _assert_no_leak(factory, pool)


async def test_cold_borrow_when_empty_grows_pool() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock, open_cost_s=3.0)
    pool = ProviderPool("dashscope", factory, clock=clock, config=_cfg(min_warm=0, max_size=2))
    # No warm sessions; borrow opens one cold.
    async with pool.borrow() as lease:
        assert str(lease.handle).startswith("handle::")
        assert pool.stats().leased == 1
        assert pool.stats().cold_borrows == 1
    assert pool.stats().warm == 1  # returned to shelf
    _assert_no_leak(factory, pool)


# --------------------------------------------------------------------------- #
# pool: exhaustion + fair waiter timeout
# --------------------------------------------------------------------------- #


async def test_borrow_exhaustion_times_out() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock, open_cost_s=0.0)
    pool = ProviderPool("dashscope", factory, clock=clock, config=_cfg(min_warm=0, max_size=1))

    # Hold the only slot.
    held = await pool.borrow(timeout_s=5.0)
    assert pool.stats().leased == 1

    # A second borrow must wait, then time out when the clock passes the deadline.
    async def second() -> None:
        async with pool.borrow(timeout_s=3.0):
            pass

    waiter = asyncio.ensure_future(second())
    await asyncio.sleep(0)  # let it park
    assert pool.stats().waiting == 1
    await clock.advance(3.0)  # past the borrow deadline
    with pytest.raises(LeaseTimeout):
        await waiter
    assert pool.stats().timeouts == 1
    # release the held lease; pool is clean.
    await pool._return(held)
    _assert_no_leak(factory, pool)


async def test_fair_handoff_serves_waiter_in_fifo_order() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock, open_cost_s=0.0)
    pool = ProviderPool("dashscope", factory, clock=clock, config=_cfg(min_warm=0, max_size=1))

    held = await pool.borrow(timeout_s=100.0)
    order: list[str] = []

    async def waiter(name: str) -> None:
        async with pool.borrow(timeout_s=100.0):
            order.append(name)
            await clock.sleep(1.0)  # hold briefly so the next waiter is served after

    w1 = asyncio.ensure_future(waiter("first"))
    await _settle()
    w2 = asyncio.ensure_future(waiter("second"))
    await _settle()
    assert pool.stats().waiting == 2

    # Return the held session → handed straight to the first waiter (FIFO).
    await pool._return(held)
    await _settle()
    assert order == ["first"]
    # first finishes its hold (sleep fires), returns → second is served (FIFO).
    await clock.advance(1.0)
    await _settle()
    await clock.advance(1.0)
    await asyncio.gather(w1, w2)
    assert order == ["first", "second"]
    _assert_no_leak(factory, pool)


# --------------------------------------------------------------------------- #
# pool: circuit-aware drain
# --------------------------------------------------------------------------- #


async def test_unhealthy_provider_drains_warm_sessions() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock)
    health = FakeHealth(available=True)
    pool = ProviderPool(
        "dashscope", factory, clock=clock, config=_cfg(min_warm=2), health=health
    )
    pool.warm_target = 2
    await pool.maintain()
    assert pool.stats().warm == 2

    # Provider circuit opens → maintain drains the warm sessions and refuses lends.
    health.set(False)
    await pool.maintain()
    st = pool.stats()
    assert st.warm == 0
    assert st.draining
    for s in factory.opened:
        assert s.closed
    with pytest.raises(PoolDraining):
        async with pool.borrow(timeout_s=1.0):
            pass

    # Circuit recovers → maintain resumes and re-warms to the target.
    health.set(True)
    await pool.maintain()
    assert pool.stats().warm == 2
    assert not pool.stats().draining
    _assert_no_leak(factory, pool)


async def test_draining_fails_parked_waiters() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock, open_cost_s=0.0)
    health = FakeHealth(available=True)
    pool = ProviderPool(
        "dashscope", factory, clock=clock, config=_cfg(min_warm=0, max_size=1), health=health
    )
    held = await pool.borrow(timeout_s=100.0)

    async def waiter() -> None:
        async with pool.borrow(timeout_s=100.0):
            pass

    w = asyncio.ensure_future(waiter())
    await asyncio.sleep(0)
    assert pool.stats().waiting == 1
    # Drain (provider unhealthy): the parked waiter is failed, not hung forever.
    health.set(False)
    await pool.maintain()
    with pytest.raises(PoolDraining):
        await w
    await pool._return(held)


# --------------------------------------------------------------------------- #
# manager: predictive pre-warm on a demand spike + keep-alive loop
# --------------------------------------------------------------------------- #


async def test_manager_prewarms_on_scheduler_hint() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock)
    mgr = WarmPoolManager(factory, config=_cfg(min_warm=1, max_warm=3), clock=clock)
    mgr.register("dashscope")

    # Baseline tick: floor of 1 warm session.
    await mgr.tick()
    assert mgr.pool("dashscope").stats().warm == 1

    # Scheduler predicts a burst (reader sped up) → hint lifts the warm target.
    mgr.hint("dashscope", renders_per_s=1.0)  # *8s horizon → clamps to max_warm 3
    await mgr.tick()
    st = mgr.pool("dashscope").stats()
    assert st.warm_target == 3
    assert st.warm == 3  # pre-warmed ahead of the first render
    _assert_no_leak(factory, mgr.pool("dashscope"))


async def test_manager_observed_demand_drives_then_decays() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock)
    mgr = WarmPoolManager(factory, config=_cfg(min_warm=1, max_warm=3), clock=clock)
    mgr.register("dashscope")

    # Several dispatches in one window → reactive target rises above the floor.
    for _ in range(3):
        mgr.record_dispatch("dashscope")
    await mgr.tick()
    assert mgr.pool("dashscope").stats().warm_target >= 2

    # Idle windows → demand decays, target relaxes back to the floor.
    for _ in range(8):
        await mgr.tick()
    assert mgr.pool("dashscope").stats().warm_target == 1


async def test_manager_cheap_provider_holds_no_idle_sessions() -> None:
    clock = VirtualClock()
    # A provider that opens instantly (no cold-start cost to hide).
    factory = FakeFactory(clock, open_cost_s=0.0)
    mgr = WarmPoolManager(factory, config=_cfg(min_warm=1, max_warm=3), clock=clock)
    pool = mgr.register("cheap")
    # Force the measured cold start to be ~free so it is not worth warming.
    pool.cost.cold = LatencyStats(mean_s=0.05, max_s=0.05, samples=5)
    pool.cost.warm = LatencyStats(mean_s=0.05, max_s=0.05, samples=5)
    await mgr.tick()
    st = pool.stats()
    assert st.warm_target == 0  # floor collapsed: cost-aware
    assert st.warm == 0
    _assert_no_leak(factory, pool)


async def test_manager_keepalive_loop_runs_on_clock() -> None:
    clock = VirtualClock()
    # open_cost_s=0 so the loop task's opens don't themselves advance the clock
    # (the loop drives the clock only via its keepalive sleep, which the test owns).
    factory = FakeFactory(clock, open_cost_s=0.0)
    mgr = WarmPoolManager(
        factory, config=_cfg(min_warm=1, keepalive_interval_s=5.0), clock=clock
    )
    mgr.register("dashscope")
    await mgr.start()
    try:
        await _settle()  # let the loop run its first tick (opens floor) + park on sleep
        assert mgr.pool("dashscope").stats().warm == 1
        # Drive a few ticks via the virtual clock; the floor is held steady.
        await clock.advance(5.0)
        await _settle()
        await clock.advance(5.0)
        await _settle()
        assert mgr.pool("dashscope").stats().warm == 1
    finally:
        await mgr.stop()
    # After stop every session is closed (no leak across shutdown).
    assert factory.live_sessions == []


async def test_manager_stop_closes_all_sessions() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock)
    mgr = WarmPoolManager(factory, config=_cfg(min_warm=2), clock=clock)
    pool = mgr.register("dashscope")
    pool.warm_target = 2
    await pool.maintain()
    assert pool.stats().warm == 2
    await mgr.stop()
    assert factory.live_sessions == []


# --------------------------------------------------------------------------- #
# open failures are tolerated (a flaky auth handshake)
# --------------------------------------------------------------------------- #


async def test_top_up_tolerates_open_failure() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock, fail_opens=1)  # first open raises
    pool = ProviderPool("dashscope", factory, clock=clock, config=_cfg(min_warm=2))
    pool.warm_target = 2
    # Should not raise; the failed open is logged and the sweep returns early.
    await pool.maintain()
    # A later sweep (factory now healthy) tops up to the floor.
    await pool.maintain()
    assert pool.stats().warm == 2
    _assert_no_leak(factory, pool)


async def test_cold_borrow_open_failure_releases_slot() -> None:
    clock = VirtualClock()
    factory = FakeFactory(clock, fail_opens=1)
    pool = ProviderPool("dashscope", factory, clock=clock, config=_cfg(min_warm=0, max_size=1))
    with pytest.raises(RuntimeError):
        async with pool.borrow(timeout_s=1.0):
            pass
    # The reserved slot was released, so the pool is empty and a retry can grow.
    assert pool.stats().total == 0
    async with pool.borrow(timeout_s=1.0):
        assert pool.stats().leased == 1
    _assert_no_leak(factory, pool)


def test_seed_cold_used_before_samples() -> None:
    m = ColdStartModel(provider="p")
    assert m.planning_cold_s == DEFAULT_SEED_COLD_S
