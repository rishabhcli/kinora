"""Unit tests for the multi-provider video router: per-backend health (pure
circuit breaker), failover ordering, racing, and — above all — that the
``LiveVideoDisabled`` spend gate is propagated unchanged and never marks a backend
unhealthy. No network, no real video."""

from __future__ import annotations

import asyncio

import pytest

from app.providers.errors import (
    LiveVideoDisabled,
    ProviderBadRequest,
    TransientProviderError,
)
from app.providers.types import VideoResult, WanMode, WanSpec
from app.providers.video_router import (
    BackendHealth,
    BackendStatus,
    BackendTier,
    RouteMode,
    RouterPolicy,
    VideoRouter,
    order_for_budget,
)

_SPEC = WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="a quiet meadow")


def _result(name: str) -> VideoResult:
    return VideoResult(
        duration_s=5.0, model=name, mode=WanMode.TEXT_TO_VIDEO, clip_bytes=b"MP4-" + name.encode()
    )


class FakeBackend:
    """A scriptable VideoBackend: each call pops the next action from ``script``."""

    def __init__(self, name: str, script: list[object], *, healthy: bool = True) -> None:
        self.name = name
        self._script = list(script)
        self._healthy = healthy
        self.calls = 0

    async def render(self, spec: WanSpec) -> VideoResult:
        self.calls += 1
        action = self._script.pop(0) if self._script else _result(self.name)
        if isinstance(action, Exception):
            raise action
        await asyncio.sleep(0)
        return action  # type: ignore[return-value]

    async def healthy(self) -> bool:
        return self._healthy


class SlowBackend:
    """A backend whose render blocks for ``delay`` seconds, then returns ``result``."""

    def __init__(self, name: str, *, delay: float, result: VideoResult | None = None) -> None:
        self.name = name
        self._delay = delay
        self._result = result or _result(name)
        self.cancelled = False

    async def render(self, spec: WanSpec) -> VideoResult:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self._result

    async def healthy(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# BackendHealth — the pure circuit breaker
# --------------------------------------------------------------------------- #


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_backend_health_trips_open_after_threshold() -> None:
    clock = FakeClock()
    health = BackendHealth(name="b", failure_threshold=3, cooldown_s=30.0, _clock=clock)
    assert health.available() is True
    health.record_failure()
    health.record_failure()
    assert health.status is BackendStatus.CLOSED  # below threshold
    health.record_failure()
    assert health.status is BackendStatus.OPEN
    assert health.available() is False  # still inside the cooldown
    assert health.total_failures == 3


def test_backend_health_half_open_probe_and_recovery() -> None:
    clock = FakeClock()
    health = BackendHealth(name="b", failure_threshold=1, cooldown_s=30.0, _clock=clock)
    health.record_failure()
    assert health.status is BackendStatus.OPEN
    clock.now = 31.0  # cooldown elapsed
    assert health.available() is True  # transitions to half-open
    assert health.status is BackendStatus.HALF_OPEN
    health.record_success()  # the probe succeeded
    assert health.status is BackendStatus.CLOSED
    assert health.consecutive_failures == 0


def test_backend_health_half_open_failure_reopens() -> None:
    clock = FakeClock()
    health = BackendHealth(name="b", failure_threshold=1, cooldown_s=10.0, _clock=clock)
    health.record_failure()
    clock.now = 11.0
    assert health.available() is True
    health.record_failure()  # half-open probe failed
    assert health.status is BackendStatus.OPEN
    assert health.available() is False


# --------------------------------------------------------------------------- #
# THE GATE — propagated unchanged, no health penalty, no second backend
# --------------------------------------------------------------------------- #


async def test_live_video_disabled_propagated_and_not_a_health_failure() -> None:
    a = FakeBackend("a", [LiveVideoDisabled("gate off")])
    b = FakeBackend("b", [_result("b")])
    router = VideoRouter([a, b])
    with pytest.raises(LiveVideoDisabled):
        await router.render(_SPEC)
    assert a.calls == 1
    assert b.calls == 0  # the gate is not a fault → no failover to a second backend
    assert router.health("a").status is BackendStatus.CLOSED  # gate is not a failure
    assert router.health("a").total_failures == 0


async def test_race_propagates_gate_and_cancels_rest() -> None:
    a = FakeBackend("a", [LiveVideoDisabled("gate off")])
    b = SlowBackend("b", delay=5.0)
    router = VideoRouter([a, b], policy=RouterPolicy(mode=RouteMode.RACE, race_size=2))
    with pytest.raises(LiveVideoDisabled):
        await router.render(_SPEC)
    assert b.cancelled is True  # the slow racer was cancelled when the gate fired


# --------------------------------------------------------------------------- #
# Failover
# --------------------------------------------------------------------------- #


async def test_failover_advances_on_retryable_error() -> None:
    a = FakeBackend("a", [TransientProviderError("5xx blip")])
    b = FakeBackend("b", [_result("b")])
    router = VideoRouter([a, b])
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-b"
    assert a.calls == 1 and b.calls == 1
    assert router.health("a").total_failures == 1
    assert router.health("b").total_successes == 1


async def test_failover_short_circuits_on_non_retryable() -> None:
    a = FakeBackend("a", [ProviderBadRequest("bad spec")])
    b = FakeBackend("b", [_result("b")])
    router = VideoRouter([a, b])
    with pytest.raises(ProviderBadRequest):
        await router.render(_SPEC)
    assert b.calls == 0  # a 4xx fails identically everywhere → don't try the next


async def test_failover_skips_open_backend() -> None:
    clock = FakeClock()
    a = FakeBackend("a", [_result("a")])
    b = FakeBackend("b", [_result("b")])
    router = VideoRouter([a, b], clock=clock)
    # Force backend "a" open.
    router.health("a").record_failure()
    router.health("a").record_failure()
    router.health("a").record_failure()
    assert router.health("a").status is BackendStatus.OPEN
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-b"  # routed straight to b
    assert a.calls == 0


async def test_failover_raises_last_error_when_all_fail() -> None:
    a = FakeBackend("a", [TransientProviderError("a down")])
    b = FakeBackend("b", [TransientProviderError("b down")])
    router = VideoRouter([a, b])
    with pytest.raises(TransientProviderError):
        await router.render(_SPEC)
    assert a.calls == 1 and b.calls == 1


async def test_all_open_falls_back_to_top_backend() -> None:
    a = FakeBackend("a", [_result("a")])
    router = VideoRouter([a], policy=RouterPolicy(failure_threshold=1))
    router.health("a").record_failure()  # now OPEN
    assert router.health("a").status is BackendStatus.OPEN
    # Even with every breaker open, the top backend gets one honest attempt.
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-a"


# --------------------------------------------------------------------------- #
# Racing
# --------------------------------------------------------------------------- #


async def test_race_first_success_wins_and_cancels_slow() -> None:
    fast = FakeBackend("fast", [_result("fast")])
    slow = SlowBackend("slow", delay=5.0)
    router = VideoRouter([fast, slow], policy=RouterPolicy(mode=RouteMode.RACE, race_size=2))
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-fast"
    assert slow.cancelled is True
    assert router.health("fast").total_successes == 1


async def test_race_loser_failure_then_winner() -> None:
    loser = FakeBackend("loser", [TransientProviderError("blip")])
    winner = SlowBackend("winner", delay=0.02)
    router = VideoRouter([loser, winner], policy=RouterPolicy(mode=RouteMode.RACE, race_size=2))
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-winner"
    assert router.health("loser").total_failures == 1


async def test_race_single_candidate_degrades_to_failover() -> None:
    only = FakeBackend("only", [_result("only")])
    router = VideoRouter([only], policy=RouterPolicy(mode=RouteMode.RACE, race_size=2))
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-only"


# --------------------------------------------------------------------------- #
# healthy() + construction guards
# --------------------------------------------------------------------------- #


async def test_router_healthy_true_when_any_backend_healthy() -> None:
    a = FakeBackend("a", [], healthy=False)
    b = FakeBackend("b", [], healthy=True)
    router = VideoRouter([a, b])
    assert await router.healthy() is True


async def test_router_healthy_false_when_none_healthy() -> None:
    a = FakeBackend("a", [], healthy=False)
    router = VideoRouter([a])
    assert await router.healthy() is False


def test_router_requires_a_backend() -> None:
    with pytest.raises(ValueError, match="at least one backend"):
        VideoRouter([])


# --------------------------------------------------------------------------- #
# Cost-aware routing (Phase 6)
# --------------------------------------------------------------------------- #


def test_order_for_budget_low_prefers_cheapest() -> None:
    turbo = FakeBackend("turbo", [])
    quality = FakeBackend("quality", [])
    tiers = {
        "turbo": BackendTier(cost_per_s=1.0, quality=0.4),
        "quality": BackendTier(cost_per_s=4.0, quality=0.9),
    }
    # quality listed first, but a low budget reorders to cheapest-first.
    ordered = order_for_budget([quality, turbo], tiers, budget_low=True)
    assert [b.name for b in ordered] == ["turbo", "quality"]


def test_order_for_budget_healthy_prefers_quality() -> None:
    turbo = FakeBackend("turbo", [])
    quality = FakeBackend("quality", [])
    tiers = {
        "turbo": BackendTier(cost_per_s=1.0, quality=0.4),
        "quality": BackendTier(cost_per_s=4.0, quality=0.9),
    }
    ordered = order_for_budget([turbo, quality], tiers, budget_low=False)
    assert [b.name for b in ordered] == ["quality", "turbo"]


def test_order_for_budget_stable_on_equal_tiers() -> None:
    a = FakeBackend("a", [])
    b = FakeBackend("b", [])
    # No tiers → neutral → input order preserved (cost-aware == failover ordering).
    assert [x.name for x in order_for_budget([a, b], {}, budget_low=True)] == ["a", "b"]
    assert [x.name for x in order_for_budget([a, b], {}, budget_low=False)] == ["a", "b"]


async def test_cost_aware_render_routes_cheapest_when_budget_low() -> None:
    turbo = FakeBackend("turbo", [_result("turbo")])
    quality = FakeBackend("quality", [_result("quality")])
    router = VideoRouter(
        [quality, turbo],  # quality preferred by priority
        policy=RouterPolicy(mode=RouteMode.COST_AWARE),
        tiers={
            "turbo": BackendTier(cost_per_s=1.0, quality=0.4),
            "quality": BackendTier(cost_per_s=4.0, quality=0.9),
        },
    )
    result = await router.render(_SPEC, budget_low=True)
    assert result.clip_bytes == b"MP4-turbo"  # cheapest chosen under low budget
    assert turbo.calls == 1 and quality.calls == 0


async def test_cost_aware_render_routes_quality_when_budget_healthy() -> None:
    turbo = FakeBackend("turbo", [_result("turbo")])
    quality = FakeBackend("quality", [_result("quality")])
    router = VideoRouter(
        [turbo, quality],
        policy=RouterPolicy(mode=RouteMode.COST_AWARE),
        tiers={
            "turbo": BackendTier(cost_per_s=1.0, quality=0.4),
            "quality": BackendTier(cost_per_s=4.0, quality=0.9),
        },
    )
    result = await router.render(_SPEC, budget_low=False)
    assert result.clip_bytes == b"MP4-quality"  # best quality when budget is fine


async def test_cost_aware_still_propagates_the_gate() -> None:
    cheap = FakeBackend("cheap", [LiveVideoDisabled("gate off")])
    other = FakeBackend("other", [_result("other")])
    router = VideoRouter(
        [other, cheap],
        policy=RouterPolicy(mode=RouteMode.COST_AWARE),
        tiers={"cheap": BackendTier(cost_per_s=1.0), "other": BackendTier(cost_per_s=2.0)},
    )
    with pytest.raises(LiveVideoDisabled):
        await router.render(_SPEC, budget_low=True)
    assert other.calls == 0  # cheapest tried, gate propagated, no failover
