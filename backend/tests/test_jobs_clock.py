"""Unit tests for the jobs clock primitives (no infra)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.jobs.clock import Clock, ManualClock, SystemClock


def test_system_clock_is_a_clock_and_returns_utc() -> None:
    clock = SystemClock()
    assert isinstance(clock, Clock)
    now = clock.now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


async def test_system_clock_sleep_zero_returns_immediately() -> None:
    await SystemClock().sleep(0)
    await SystemClock().sleep(-5)  # clamped to 0, no error


def test_manual_clock_starts_at_fixed_default() -> None:
    clock = ManualClock()
    assert clock.now() == datetime(2026, 1, 1, tzinfo=UTC)


def test_manual_clock_naive_start_is_made_utc() -> None:
    clock = ManualClock(start=datetime(2030, 5, 5))  # naive
    assert clock.now().tzinfo == UTC


async def test_manual_clock_advance_moves_time() -> None:
    clock = ManualClock()
    start = clock.now()
    await clock.advance(90)
    assert clock.now() == start + timedelta(seconds=90)


async def test_manual_clock_advance_negative_raises() -> None:
    clock = ManualClock()
    with pytest.raises(ValueError, match="backwards"):
        await clock.advance(-1)


async def test_manual_clock_sleep_blocks_until_advance() -> None:
    clock = ManualClock()
    woke = asyncio.Event()

    async def sleeper() -> None:
        await clock.sleep(10)
        woke.set()

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)  # let the sleeper park
    assert clock.pending_sleepers == 1
    assert not woke.is_set()

    await clock.advance(5)
    assert not woke.is_set()  # not enough time yet
    assert clock.pending_sleepers == 1

    await clock.advance(5)
    await task
    assert woke.is_set()
    assert clock.pending_sleepers == 0


async def test_manual_clock_zero_sleep_yields_but_does_not_park() -> None:
    clock = ManualClock()
    await clock.sleep(0)
    assert clock.pending_sleepers == 0


async def test_manual_clock_advance_to_absolute_instant() -> None:
    clock = ManualClock(start=datetime(2026, 1, 1, tzinfo=UTC))
    target = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    woke = asyncio.Event()

    async def sleeper() -> None:
        await clock.sleep(1800)  # 30 min
        woke.set()

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)
    await clock.advance_to(target)
    await task
    assert woke.is_set()
    assert clock.now() == target


async def test_manual_clock_advance_to_past_is_noop_but_wakes_due() -> None:
    clock = ManualClock(start=datetime(2026, 6, 1, tzinfo=UTC))
    before = clock.now()
    await clock.advance_to(datetime(2026, 1, 1, tzinfo=UTC))
    assert clock.now() == before
