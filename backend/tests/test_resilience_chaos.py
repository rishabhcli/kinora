"""Tests for the chaos harness — deterministic injection + the prod safety gate."""

from __future__ import annotations

import random

import pytest

from app.resilience.chaos import (
    ChaosConfig,
    ChaosFault,
    ChaosMonkey,
    chaos_from_settings,
)
from app.resilience.clock import ManualClock
from app.resilience.errors import (
    AuthError,
    CallTimeout,
    ChaosInjectedError,
    PermanentError,
    RateLimitedError,
)


async def test_disabled_monkey_is_passthrough() -> None:
    monkey = ChaosMonkey(
        "m", ChaosConfig(fault_probability=1.0), enabled=False, rng=random.Random(0)
    )
    await monkey.before_call()  # never raises
    assert monkey.faults_injected == 0


async def test_armed_monkey_injects_transient_by_default() -> None:
    monkey = ChaosMonkey(
        "m", ChaosConfig(fault_probability=1.0), enabled=True, rng=random.Random(0)
    )
    with pytest.raises(ChaosInjectedError):
        await monkey.before_call()
    assert monkey.faults_injected == 1


async def test_fault_weights_select_kind() -> None:
    cfg = ChaosConfig(
        fault_probability=1.0, fault_weights={ChaosFault.THROTTLE: 1.0}
    )
    monkey = ChaosMonkey("m", cfg, enabled=True, rng=random.Random(1))
    with pytest.raises(RateLimitedError) as ei:
        await monkey.before_call()
    assert ei.value.retry_after_s == cfg.throttle_retry_after_s


async def test_each_fault_kind_maps_to_taxonomy() -> None:
    for kind, exc in [
        (ChaosFault.TRANSIENT, ChaosInjectedError),
        (ChaosFault.TIMEOUT, CallTimeout),
        (ChaosFault.THROTTLE, RateLimitedError),
        (ChaosFault.PERMANENT, PermanentError),
        (ChaosFault.AUTH, AuthError),
    ]:
        monkey = ChaosMonkey(
            "m",
            ChaosConfig(fault_probability=1.0, fault_weights={kind: 1.0}),
            enabled=True,
            rng=random.Random(0),
        )
        with pytest.raises(exc):
            await monkey.before_call()


async def test_latency_injection_advances_clock() -> None:
    clock = ManualClock()
    cfg = ChaosConfig(
        fault_probability=0.0,
        latency_probability=1.0,
        latency_min_s=2.0,
        latency_max_s=2.0,
    )
    monkey = ChaosMonkey("m", cfg, enabled=True, rng=random.Random(0), clock=clock)
    await monkey.before_call()
    assert monkey.latencies_injected == 1
    assert clock.monotonic() == pytest.approx(2.0)


async def test_zero_probability_never_injects() -> None:
    monkey = ChaosMonkey(
        "m", ChaosConfig(fault_probability=0.0), enabled=True, rng=random.Random(0)
    )
    for _ in range(50):
        await monkey.before_call()
    assert monkey.faults_injected == 0


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        ChaosConfig(fault_probability=1.5)
    with pytest.raises(ValueError):
        ChaosConfig(latency_min_s=5.0, latency_max_s=1.0)
    with pytest.raises(ValueError):
        ChaosConfig(fault_weights={ChaosFault.TRANSIENT: -1.0})


def test_from_settings_disabled_by_default() -> None:
    monkey = chaos_from_settings("m")
    assert monkey.enabled is False


def test_from_settings_refuses_to_arm_outside_local(monkeypatch) -> None:
    from app.core import config as config_mod

    class _FakeSettings:
        app_env = "production"
        is_local = False
        resilience_chaos_enabled = True
        resilience_chaos_fault_probability = 1.0
        resilience_chaos_latency_probability = 0.0
        resilience_chaos_latency_min_s = 0.0
        resilience_chaos_latency_max_s = 0.0

    monkeypatch.setattr(config_mod, "get_settings", lambda: _FakeSettings())
    monkey = chaos_from_settings("m")
    assert monkey.enabled is False  # refused despite the flag being True


def test_from_settings_arms_in_local_when_enabled(monkeypatch) -> None:
    from app.core import config as config_mod

    class _FakeSettings:
        app_env = "local"
        is_local = True
        resilience_chaos_enabled = True
        resilience_chaos_fault_probability = 0.25
        resilience_chaos_latency_probability = 0.0
        resilience_chaos_latency_min_s = 0.0
        resilience_chaos_latency_max_s = 0.0

    monkeypatch.setattr(config_mod, "get_settings", lambda: _FakeSettings())
    monkey = chaos_from_settings("m")
    assert monkey.enabled is True
    assert monkey.config.fault_probability == 0.25


async def test_seeded_injection_is_reproducible() -> None:
    cfg = ChaosConfig(fault_probability=0.5)

    async def run() -> int:
        monkey = ChaosMonkey("m", cfg, enabled=True, rng=random.Random(123))
        injected = 0
        for _ in range(40):
            try:
                await monkey.before_call()
            except ChaosInjectedError:
                injected += 1
        return injected

    assert await run() == await run()
