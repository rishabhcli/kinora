"""Deterministic tests for the chaos virtual clock."""

from __future__ import annotations

import pytest

from app.chaos.clock import SystemClock, VirtualClock


def test_virtual_clock_advances_only_when_told() -> None:
    clock = VirtualClock(start=1000.0)
    assert clock.time() == 1000.0
    assert clock.monotonic() == 0.0
    clock.advance(5.0)
    assert clock.time() == 1005.0
    assert clock.monotonic() == 5.0


def test_virtual_clock_refuses_to_go_backwards() -> None:
    clock = VirtualClock()
    with pytest.raises(ValueError):
        clock.advance(-1.0)


async def test_virtual_sleep_advances_instantly_no_real_wait() -> None:
    clock = VirtualClock(start=0.0)
    await clock.sleep(30.0)
    # The sleep advanced the virtual clock without any real timer.
    assert clock.time() == 30.0
    assert clock.slept_for == 30.0


async def test_virtual_sleep_negative_clamped_to_zero() -> None:
    clock = VirtualClock(start=0.0)
    await clock.sleep(-5.0)
    assert clock.time() == 0.0
    assert clock.slept_for == 0.0


def test_system_clock_monotonic_nondecreasing() -> None:
    clock = SystemClock()
    a = clock.monotonic()
    b = clock.monotonic()
    assert b >= a
