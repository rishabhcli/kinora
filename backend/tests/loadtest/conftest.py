"""Shared helpers for the load-test suite: drive a coroutine on a VirtualClock."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.loadtest.clock import VirtualClock

T = TypeVar("T")


def drive(clock: VirtualClock, make_coro: Callable[[], Awaitable[T]]) -> T:
    """Run ``make_coro()`` to completion under ``clock`` in zero real time.

    The same clock instance must be the one injected into whatever the coroutine
    drives (generator / target), so the driver and the workload share one
    timeline. ``make_coro`` is a thunk so the coroutine is created inside the
    event loop the driver owns.
    """
    import asyncio

    return asyncio.run(clock.run(make_coro()))
