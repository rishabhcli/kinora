"""Unit tests for the per-provider throughput gate: concurrency cap + token-bucket
rate limit. Time-injected (fake clock + fake sleep) so saturation and refill are
deterministic without real waits."""

from __future__ import annotations

import asyncio

import pytest

from app.video.routing.concurrency import (
    GateBook,
    GateConfig,
    ProviderGate,
    TokenBucket,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_gate_config_validates() -> None:
    with pytest.raises(ValueError):
        GateConfig(max_concurrency=0)
    with pytest.raises(ValueError):
        GateConfig(rate_per_s=-1.0)
    with pytest.raises(ValueError):
        GateConfig(burst=-1)


async def test_token_bucket_noop_when_rate_zero() -> None:
    bucket = TokenBucket(rate_per_s=0.0, burst=1)
    # Should never block; acquire many times instantly.
    for _ in range(100):
        await bucket.acquire()


async def test_token_bucket_blocks_then_refills() -> None:
    clock = FakeClock()
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        clock.now += s  # advancing the clock refills the bucket

    bucket = TokenBucket(rate_per_s=2.0, burst=2, clock=clock, sleep=fake_sleep)
    # Burst of 2 is free.
    await bucket.acquire()
    await bucket.acquire()
    assert slept == []
    # Third acquire must wait for a refill (1 token / 0.5s at rate 2/s).
    await bucket.acquire()
    assert slept and slept[0] == pytest.approx(0.5)


async def test_gate_caps_concurrency() -> None:
    gate = ProviderGate(GateConfig(max_concurrency=2))
    started = asyncio.Event()
    release = asyncio.Event()
    peak = 0

    async def worker() -> None:
        nonlocal peak
        async with gate.slot():
            peak = max(peak, gate.in_flight)
            started.set()
            await release.wait()

    tasks = [asyncio.ensure_future(worker()) for _ in range(4)]
    await started.wait()
    await asyncio.sleep(0)  # let the schedulable workers grab slots
    assert gate.in_flight == 2  # only 2 can hold a slot at once
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2
    assert gate.in_flight == 0


async def test_gate_book_default_and_named() -> None:
    book = GateBook(
        {"fast": GateConfig(max_concurrency=8)},
        default=GateConfig(max_concurrency=1),
    )
    assert book.gate("fast").config.max_concurrency == 8
    # An unconfigured name falls back to the default gate.
    assert book.gate("other").config.max_concurrency == 1
    # The same name returns the same gate instance (state persists).
    assert book.gate("other") is book.gate("other")
