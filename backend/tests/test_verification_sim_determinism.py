"""Entropy ownership + the runtime bridge (the last mile of byte-identical replay).

Proves ``deterministic_entropy`` redirects the production code's ``uuid``/``random``/
``time`` to the seed *and restores them on exit*, and that ``Simulation.run_sync`` /
``run_resilient`` drive coroutines deterministically on the virtual clock.
"""

from __future__ import annotations

import random
import time
import uuid

from app.verification.simulation.core import Prng
from app.verification.simulation.determinism import deterministic_entropy
from app.verification.simulation.faults import FaultProfile, FaultSchedule
from app.verification.simulation.redis_sim import SimRedisError
from app.verification.simulation.runtime import Simulation


def test_deterministic_entropy_makes_uuid_stable() -> None:
    def ids() -> list[str]:
        with deterministic_entropy(Prng(42)):
            return [uuid.uuid4().hex for _ in range(5)]

    assert ids() == ids()  # same seed → same uuids


def test_deterministic_entropy_restores_globals() -> None:
    real_uuid = uuid.uuid4
    real_time = time.time
    real_monotonic = time.monotonic
    with deterministic_entropy(Prng(1), now_ms=lambda: 5_000):
        assert time.time() == 5.0  # virtual time inside the block
        assert uuid.uuid4 is not real_uuid
    # Everything restored on exit.
    assert uuid.uuid4 is real_uuid
    assert time.time is real_time
    assert time.monotonic is real_monotonic


def test_deterministic_entropy_seeds_global_random() -> None:
    def draws() -> list[float]:
        with deterministic_entropy(Prng(7)):
            return [random.random() for _ in range(5)]

    assert draws() == draws()


def test_deterministic_entropy_restores_random_state_on_exception() -> None:
    state_before = random.getstate()
    try:
        with deterministic_entropy(Prng(3)):
            random.random()
            raise ValueError("boom")
    except ValueError:
        pass
    assert random.getstate() == state_before


def test_runtime_run_sync_executes_coroutine_now() -> None:
    sim = Simulation(FaultSchedule(seed=1, profile=FaultProfile.calm()))
    try:

        async def add(a: int, b: int) -> int:
            return a + b

        assert sim.run_sync(add(2, 3)) == 5
    finally:
        sim.close()


def test_runtime_streams_are_independent_and_stable() -> None:
    sim = Simulation(FaultSchedule(seed=99, profile=FaultProfile.calm()))
    try:
        a1 = sim.stream("worker")
        a2 = sim.stream("worker")  # same label → same stream object
        assert a1 is a2
        b = sim.stream("reader")
        assert a1.random() != b.random()  # different labels diverge
    finally:
        sim.close()


def test_runtime_run_resilient_retries_then_returns_default() -> None:
    sim = Simulation(FaultSchedule(seed=1, profile=FaultProfile.calm()))
    try:
        calls = [0]

        async def always_fails() -> int:
            calls[0] += 1
            raise SimRedisError("blip")

        t0 = sim.now_ms
        out = sim.run_resilient(
            always_fails, transient=(SimRedisError,), attempts=4, backoff_ms=10, default=-1
        )
        assert out == -1  # gave up gracefully
        assert calls[0] == 4  # retried the configured number of times
        assert sim.now_ms == t0 + 10 * 3  # advanced virtual clock between retries
    finally:
        sim.close()


def test_runtime_run_resilient_succeeds_after_transient() -> None:
    sim = Simulation(FaultSchedule(seed=1, profile=FaultProfile.calm()))
    try:
        attempts = [0]

        async def flaky() -> str:
            attempts[0] += 1
            if attempts[0] < 3:
                raise SimRedisError("blip")
            return "ok"

        assert sim.run_resilient(flaky, transient=(SimRedisError,)) == "ok"
        assert attempts[0] == 3
    finally:
        sim.close()
