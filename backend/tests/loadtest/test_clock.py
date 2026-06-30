"""VirtualClock determinism + discrete-event ordering (no real sleeping)."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.loadtest.clock import VirtualClock, WallClock


@pytest.mark.asyncio
async def test_wall_clock_now_monotonic_and_sleeps() -> None:
    clock = WallClock()
    t0 = clock.now()
    await clock.sleep(0.0)  # non-positive returns promptly
    t1 = clock.now()
    assert t1 >= t0


def test_virtual_clock_advances_only_by_sleeps() -> None:
    clock = VirtualClock()

    async def body() -> float:
        assert clock.now() == 0.0
        await clock.sleep(2.5)
        assert clock.now() == pytest.approx(2.5)
        await clock.sleep(1.0)
        return clock.now()

    end = asyncio.run(clock.run(body()))
    assert end == pytest.approx(3.5)


def test_virtual_clock_zero_time_no_real_wait() -> None:
    """A long virtual schedule runs in negligible wall time."""
    clock = VirtualClock()

    async def body() -> None:
        for _ in range(50):
            await clock.sleep(3600.0)  # 50 hours of virtual time

    wall0 = time.monotonic()
    asyncio.run(clock.run(body()))
    wall = time.monotonic() - wall0
    assert clock.now() == pytest.approx(50 * 3600.0)
    assert wall < 1.0  # 50 virtual hours, well under a second of real time


def test_virtual_clock_orders_concurrent_sleepers_by_wake_time() -> None:
    clock = VirtualClock()
    order: list[tuple[str, float]] = []

    async def waker(name: str, dt: float) -> None:
        await clock.sleep(dt)
        order.append((name, clock.now()))

    async def main() -> None:
        await asyncio.gather(
            waker("c", 3.0),
            waker("a", 1.0),
            waker("b", 2.0),
        )

    asyncio.run(clock.run(main()))
    # Wake order follows wake time, not launch order.
    assert [n for n, _ in order] == ["a", "b", "c"]
    assert [t for _, t in order] == pytest.approx([1.0, 2.0, 3.0])


def test_virtual_clock_simultaneous_wakeups_fire_together() -> None:
    clock = VirtualClock()
    fired_at: list[float] = []

    async def waker() -> None:
        await clock.sleep(5.0)
        fired_at.append(clock.now())

    async def main() -> None:
        await asyncio.gather(*(waker() for _ in range(4)))

    asyncio.run(clock.run(main()))
    assert fired_at == pytest.approx([5.0, 5.0, 5.0, 5.0])


def test_virtual_clock_is_reproducible_across_runs() -> None:
    """Same workload + same structure ⇒ identical timeline every run."""

    def run_once() -> list[float]:
        clock = VirtualClock()
        stamps: list[float] = []

        async def worker(i: int) -> None:
            for k in range(3):
                await clock.sleep(1.0 + i * 0.5 + k)
                stamps.append(round(clock.now(), 6))

        async def main() -> None:
            await asyncio.gather(*(worker(i) for i in range(3)))

        asyncio.run(clock.run(main()))
        return stamps

    a = run_once()
    b = run_once()
    assert a == b
