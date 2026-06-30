"""Integration tests for the v2 RoutingVideoRouter: failover, hedging/racing, the
breaker, sticky routing, capability filtering, the budget guard for hedging, and —
above all — that the LiveVideoDisabled spend gate is propagated unchanged and never
a health failure. Deterministic fakes; no network, no real video."""

from __future__ import annotations

import asyncio

import pytest

from app.providers.errors import (
    LiveVideoDisabled,
    ProviderBadRequest,
    ProviderError,
    TransientProviderError,
)
from app.providers.types import VideoResult, WanMode, WanSpec
from app.video.routing.capabilities import ProviderProfile
from app.video.routing.concurrency import GateConfig
from app.video.routing.health import CircuitState, HealthConfig
from app.video.routing.policy import (
    CheapestCapablePolicy,
    HighestQualityPolicy,
)
from app.video.routing.router import RouterV2Policy, RoutingVideoRouter

_SPEC = WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="a quiet meadow")


def _result(name: str) -> VideoResult:
    return VideoResult(
        duration_s=5.0, model=name, mode=WanMode.TEXT_TO_VIDEO, clip_bytes=b"MP4-" + name.encode()
    )


class FakeBackend:
    """A scriptable VideoBackend: each render pops the next action from ``script``."""

    def __init__(self, name: str, script: list[object] | None = None, *, healthy: bool = True):
        self.name = name
        self._script = list(script or [])
        self._healthy = healthy
        self.calls = 0

    async def render(self, spec: WanSpec) -> VideoResult:
        self.calls += 1
        action = self._script.pop(0) if self._script else _result(self.name)
        await asyncio.sleep(0)
        if isinstance(action, Exception):
            raise action
        return action  # type: ignore[return-value]

    async def healthy(self) -> bool:
        return self._healthy


class SlowBackend:
    """A backend whose render blocks for ``delay`` then returns; tracks cancellation."""

    def __init__(self, name: str, *, delay: float, result: VideoResult | None = None):
        self.name = name
        self._delay = delay
        self._result = result or _result(name)
        self.cancelled = False
        self.calls = 0

    async def render(self, spec: WanSpec) -> VideoResult:
        self.calls += 1
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self._result

    async def healthy(self) -> bool:
        return True


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _router(backends: list[object], **kw: object) -> RoutingVideoRouter:
    return RoutingVideoRouter(backends, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# THE GATE — propagated unchanged, no health penalty, no failover
# --------------------------------------------------------------------------- #


async def test_live_video_disabled_propagated_no_health_no_failover() -> None:
    a = FakeBackend("a", [LiveVideoDisabled("gate off")])
    b = FakeBackend("b", [_result("b")])
    router = _router([a, b], policy=RouterV2Policy(selection=CheapestCapablePolicy(), sticky=False))
    with pytest.raises(LiveVideoDisabled):
        await router.render(_SPEC)
    assert a.calls == 1
    assert b.calls == 0  # gate is not a fault -> no failover
    assert router.health("a").state is CircuitState.CLOSED
    assert router.health("a").total_failures == 0
    assert router.metrics.gate_propagations == 1


async def test_hedge_propagates_gate_and_cancels_losers() -> None:
    a = FakeBackend("a", [LiveVideoDisabled("gate off")])
    slow = SlowBackend("b", delay=5.0)
    router = _router(
        [a, slow],
        policy=RouterV2Policy(selection=CheapestCapablePolicy(), hedge=2, sticky=False),
    )
    with pytest.raises(LiveVideoDisabled):
        await router.render(_SPEC)
    assert slow.cancelled is True


# --------------------------------------------------------------------------- #
# Failover
# --------------------------------------------------------------------------- #


async def test_failover_advances_on_retryable() -> None:
    a = FakeBackend("a", [TransientProviderError("5xx")])
    b = FakeBackend("b", [_result("b")])
    router = _router([a, b], policy=RouterV2Policy(selection=CheapestCapablePolicy(), sticky=False))
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-b"
    assert a.calls == 1 and b.calls == 1
    assert router.health("a").total_failures == 1
    assert router.health("b").total_successes == 1
    assert router.metrics.failovers == 1


async def test_failover_short_circuits_on_non_retryable() -> None:
    a = FakeBackend("a", [ProviderBadRequest("bad spec")])
    b = FakeBackend("b", [_result("b")])
    router = _router([a, b], policy=RouterV2Policy(selection=CheapestCapablePolicy(), sticky=False))
    with pytest.raises(ProviderBadRequest):
        await router.render(_SPEC)
    assert b.calls == 0  # 4xx fails identically everywhere


async def test_failover_raises_last_error_when_all_fail() -> None:
    a = FakeBackend("a", [TransientProviderError("a down")])
    b = FakeBackend("b", [TransientProviderError("b down")])
    router = _router([a, b], policy=RouterV2Policy(selection=CheapestCapablePolicy(), sticky=False))
    with pytest.raises(TransientProviderError):
        await router.render(_SPEC)
    assert a.calls == 1 and b.calls == 1
    assert router.metrics.hard_errors == 1


async def test_failover_skips_open_breaker() -> None:
    clock = FakeClock()
    a = FakeBackend("a", [_result("a")])
    b = FakeBackend("b", [_result("b")])
    router = _router(
        [a, b],
        clock=clock,
        policy=RouterV2Policy(
            selection=CheapestCapablePolicy(),
            sticky=False,
            health=HealthConfig(failure_threshold=1, base_cooldown_s=30.0),
        ),
    )
    router.health("a").record_failure()  # force a OPEN
    assert router.health("a").state is CircuitState.OPEN
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-b"
    assert a.calls == 0
    assert router.health("a").total_rejections >= 1


async def test_all_open_falls_back_to_top_backend() -> None:
    a = FakeBackend("a", [_result("a")])
    router = _router(
        [a],
        policy=RouterV2Policy(
            selection=CheapestCapablePolicy(),
            sticky=False,
            health=HealthConfig(failure_threshold=1),
        ),
    )
    router.health("a").record_failure()  # OPEN
    result = await router.render(_SPEC)  # still gets one honest attempt
    assert result.clip_bytes == b"MP4-a"


async def test_max_failover_attempts_caps_tries() -> None:
    a = FakeBackend("a", [TransientProviderError("x")])
    b = FakeBackend("b", [TransientProviderError("y")])
    c = FakeBackend("c", [_result("c")])
    router = _router(
        [a, b, c],
        policy=RouterV2Policy(
            selection=CheapestCapablePolicy(), sticky=False, max_failover_attempts=2
        ),
    )
    with pytest.raises(TransientProviderError):
        await router.render(_SPEC)
    assert c.calls == 0  # capped at 2 attempts, never reached c


# --------------------------------------------------------------------------- #
# Breaker transitions through the router
# --------------------------------------------------------------------------- #


async def test_breaker_trips_then_recovers_through_router() -> None:
    clock = FakeClock()
    a = FakeBackend(
        "a",
        [
            TransientProviderError("1"),
            TransientProviderError("2"),
            _result("a"),  # the eventual recovery probe succeeds
        ],
    )
    b = FakeBackend("b", [_result("b"), _result("b")])
    router = _router(
        [a, b],
        clock=clock,
        policy=RouterV2Policy(
            selection=CheapestCapablePolicy(),
            sticky=False,
            health=HealthConfig(failure_threshold=2, base_cooldown_s=10.0),
        ),
    )
    # Two failures on a trip it OPEN (failover to b each time).
    await router.render(_SPEC)  # a fails #1, b serves
    await router.render(_SPEC)  # a fails #2 -> OPEN, b serves
    assert router.health("a").state is CircuitState.OPEN
    # Cooldown elapses -> half-open probe -> a recovers.
    clock.now = 11.0
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-a"  # cheapest a is healthy again
    assert router.health("a").state is CircuitState.CLOSED


# --------------------------------------------------------------------------- #
# Hedging / racing
# --------------------------------------------------------------------------- #


async def test_hedge_first_success_wins_and_cancels_slow() -> None:
    fast = FakeBackend("fast", [_result("fast")])
    slow = SlowBackend("slow", delay=5.0)
    # equal cost so both race in priority order; hedge=2 starts both.
    router = _router(
        [fast, slow],
        policy=RouterV2Policy(selection=CheapestCapablePolicy(), hedge=2, sticky=False),
        profiles={"fast": ProviderProfile(cost_per_s=1.0), "slow": ProviderProfile(cost_per_s=1.0)},
    )
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-fast"
    assert slow.cancelled is True
    assert router.health("fast").total_successes == 1
    assert router.metrics.hedges_launched == 1


async def test_hedge_loser_failure_then_winner() -> None:
    loser = FakeBackend("loser", [TransientProviderError("blip")])
    winner = SlowBackend("winner", delay=0.02)
    router = _router(
        [loser, winner],
        policy=RouterV2Policy(selection=CheapestCapablePolicy(), hedge=2, sticky=False),
        profiles={
            "loser": ProviderProfile(cost_per_s=1.0),
            "winner": ProviderProfile(cost_per_s=1.0),
        },
    )
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-winner"
    assert router.health("loser").total_failures == 1


async def test_hedge_non_retryable_aborts_field() -> None:
    bad = FakeBackend("bad", [ProviderBadRequest("bad spec")])
    slow = SlowBackend("slow", delay=5.0)
    router = _router(
        [bad, slow],
        policy=RouterV2Policy(selection=CheapestCapablePolicy(), hedge=2, sticky=False),
        profiles={"bad": ProviderProfile(cost_per_s=1.0), "slow": ProviderProfile(cost_per_s=1.0)},
    )
    with pytest.raises(ProviderBadRequest):
        await router.render(_SPEC)
    assert slow.cancelled is True  # the loser is cancelled when the field aborts


async def test_hedge_all_fail_then_falls_back_to_unraced() -> None:
    # hedge=2 over 3 backends: first two fail retryably, the third (un-raced) serves.
    a = FakeBackend("a", [TransientProviderError("a")])
    b = FakeBackend("b", [TransientProviderError("b")])
    c = FakeBackend("c", [_result("c")])
    router = _router(
        [a, b, c],
        policy=RouterV2Policy(selection=CheapestCapablePolicy(), hedge=2, sticky=False),
        profiles={
            "a": ProviderProfile(cost_per_s=1.0),
            "b": ProviderProfile(cost_per_s=2.0),
            "c": ProviderProfile(cost_per_s=3.0),
        },
    )
    result = await router.render(_SPEC)
    assert result.clip_bytes == b"MP4-c"
    assert a.calls == 1 and b.calls == 1 and c.calls == 1


# --------------------------------------------------------------------------- #
# Budget guard for hedging
# --------------------------------------------------------------------------- #


async def test_budget_low_disables_hedge_by_default() -> None:
    # hedge=2 normally, but budget_low collapses to a single failover attempt so a
    # budget-constrained render never spends double video-seconds.
    fast = FakeBackend("fast", [_result("fast")])
    slow = SlowBackend("slow", delay=5.0)
    router = _router(
        [fast, slow],
        policy=RouterV2Policy(selection=CheapestCapablePolicy(), hedge=2, sticky=False),
        profiles={"fast": ProviderProfile(cost_per_s=1.0), "slow": ProviderProfile(cost_per_s=2.0)},
    )
    result = await router.render(_SPEC, budget_low=True)
    assert result.clip_bytes == b"MP4-fast"
    assert slow.calls == 0  # never even started -> no double spend
    assert router.metrics.hedges_launched == 0


async def test_hedge_when_budget_low_opt_in() -> None:
    fast = FakeBackend("fast", [_result("fast")])
    slow = SlowBackend("slow", delay=5.0)
    router = _router(
        [fast, slow],
        policy=RouterV2Policy(
            selection=CheapestCapablePolicy(), hedge=2, hedge_when_budget_low=True, sticky=False
        ),
        profiles={"fast": ProviderProfile(cost_per_s=1.0), "slow": ProviderProfile(cost_per_s=1.0)},
    )
    result = await router.render(_SPEC, budget_low=True)
    assert result.clip_bytes == b"MP4-fast"
    assert slow.cancelled is True  # opt-in: hedge fired even under budget pressure


# --------------------------------------------------------------------------- #
# Sticky routing
# --------------------------------------------------------------------------- #


async def test_sticky_pins_family_to_first_winner() -> None:
    # quality is preferred by the policy, but once turbo serves a family it sticks.
    turbo = FakeBackend("turbo", [_result("turbo"), _result("turbo")])
    quality = FakeBackend("quality", [_result("quality"), _result("quality")])
    router = _router(
        [turbo, quality],
        policy=RouterV2Policy(selection=HighestQualityPolicy(), sticky=True),
        profiles={
            "turbo": ProviderProfile(cost_per_s=1.0, quality=0.4),
            "quality": ProviderProfile(cost_per_s=4.0, quality=0.9),
        },
    )
    spec1 = WanSpec(mode=WanMode.TEXT_TO_VIDEO, shot_id="seg1:shot1")
    spec2 = WanSpec(mode=WanMode.TEXT_TO_VIDEO, shot_id="seg1:shot2")
    # First shot: quality wins (highest-quality policy) and the family pins to it.
    r1 = await router.render(spec1)
    assert r1.model == "quality"
    # Second shot in the same family: stickiness registers a hit (the pinned
    # backend is still routable and is promoted to the front).
    r2 = await router.render(spec2)
    assert r2.model == "quality"
    assert router.metrics.sticky_hits >= 1


async def test_sticky_never_resurrects_open_backend() -> None:
    clock = FakeClock()
    a = FakeBackend("a", [_result("a")])
    b = FakeBackend("b", [_result("b"), _result("b")])
    router = _router(
        [a, b],
        clock=clock,
        policy=RouterV2Policy(
            selection=CheapestCapablePolicy(),
            sticky=True,
            health=HealthConfig(failure_threshold=1, base_cooldown_s=100.0),
        ),
        profiles={"a": ProviderProfile(cost_per_s=1.0), "b": ProviderProfile(cost_per_s=2.0)},
    )
    spec = WanSpec(mode=WanMode.TEXT_TO_VIDEO, shot_id="seg9:shot1")
    spec2 = WanSpec(mode=WanMode.TEXT_TO_VIDEO, shot_id="seg9:shot2")
    r1 = await router.render(spec)  # cheapest a serves and pins
    assert r1.model == "a"
    router.health("a").record_failure()  # force a OPEN
    r2 = await router.render(spec2)  # a is unroutable; sticky must not resurrect it
    assert r2.model == "b"


# --------------------------------------------------------------------------- #
# Capability filtering
# --------------------------------------------------------------------------- #


async def test_capability_filter_routes_only_capable() -> None:
    t2v_only = FakeBackend("t2v", [_result("t2v")])
    full = FakeBackend("full", [_result("full")])
    router = _router(
        [t2v_only, full],
        policy=RouterV2Policy(selection=CheapestCapablePolicy(), sticky=False),
        profiles={
            "t2v": ProviderProfile(modes=frozenset({WanMode.TEXT_TO_VIDEO}), cost_per_s=1.0),
            "full": ProviderProfile(modes=frozenset(WanMode), cost_per_s=4.0),
        },
    )
    i2v_spec = WanSpec(mode=WanMode.IMAGE_TO_VIDEO, image_url="data:image/png;base64,AAA")
    result = await router.render(i2v_spec)
    assert result.model == "full"  # t2v-only filtered out despite being cheaper
    assert t2v_only.calls == 0


async def test_no_capable_backend_raises() -> None:
    t2v_only = FakeBackend("t2v", [_result("t2v")])
    router = _router(
        [t2v_only],
        policy=RouterV2Policy(selection=CheapestCapablePolicy(), sticky=False),
        profiles={"t2v": ProviderProfile(modes=frozenset({WanMode.TEXT_TO_VIDEO}))},
    )
    ref_spec = WanSpec(mode=WanMode.REFERENCE_TO_VIDEO, reference_image_urls=["data:x"])
    with pytest.raises(ProviderError, match="no routable backend"):
        await router.render(ref_spec)
    assert router.metrics.no_capable == 1


# --------------------------------------------------------------------------- #
# Concurrency gate is honored end-to-end
# --------------------------------------------------------------------------- #


async def test_per_backend_concurrency_gate() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    peak = 0

    class GatedBackend:
        name = "gated"

        async def render(self, spec: WanSpec) -> VideoResult:
            nonlocal peak
            started.set()
            await release.wait()
            return _result("gated")

        async def healthy(self) -> bool:
            return True

    gated = GatedBackend()
    router = _router(
        [gated],
        policy=RouterV2Policy(selection=CheapestCapablePolicy(), sticky=False),
        gates={"gated": GateConfig(max_concurrency=1)},
    )
    t1 = asyncio.ensure_future(router.render(_SPEC))
    await started.wait()
    # A second render must block on the gate (in_flight capped at 1).
    t2 = asyncio.ensure_future(router.render(_SPEC))
    await asyncio.sleep(0)
    assert router._gates.gate("gated").in_flight == 1  # noqa: SLF001 - white-box check
    release.set()
    await asyncio.gather(t1, t2)


# --------------------------------------------------------------------------- #
# healthy() + construction guards + metrics snapshot
# --------------------------------------------------------------------------- #


async def test_router_healthy_true_when_any_backend_healthy() -> None:
    a = FakeBackend("a", healthy=False)
    b = FakeBackend("b", healthy=True)
    router = _router([a, b], policy=RouterV2Policy(sticky=False))
    assert await router.healthy() is True


async def test_router_healthy_false_when_none_healthy() -> None:
    a = FakeBackend("a", healthy=False)
    router = _router([a], policy=RouterV2Policy(sticky=False))
    assert await router.healthy() is False


def test_router_requires_a_backend() -> None:
    with pytest.raises(ValueError, match="at least one backend"):
        RoutingVideoRouter([])


def test_router_rejects_duplicate_names() -> None:
    a = FakeBackend("dup")
    b = FakeBackend("dup")
    with pytest.raises(ValueError, match="duplicate backend name"):
        RoutingVideoRouter([a, b])


async def test_health_snapshot_jsonable() -> None:
    a = FakeBackend("a", [_result("a")])
    router = _router([a], policy=RouterV2Policy(selection=CheapestCapablePolicy(), sticky=False))
    await router.render(_SPEC)
    snap = router.health_snapshot()
    assert snap["router"] == "video-router-v2"
    assert isinstance(snap["backends"], list)
    assert isinstance(snap["metrics"], dict)
    assert router.metrics.successes == 1


async def test_router_is_drop_in_video_backend() -> None:
    # The v2 router satisfies the VideoBackend protocol so it nests / drops in.
    from app.providers.video_router import VideoBackend

    a = FakeBackend("a", [_result("a")])
    router = _router([a], policy=RouterV2Policy(sticky=False))
    assert isinstance(router, VideoBackend)
