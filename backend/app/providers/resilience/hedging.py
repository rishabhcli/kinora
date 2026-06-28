"""Hedged / duplicate requests for tail-latency cuts.

A "hedged request" (Dean & Barroso, *The Tail at Scale*) fires a second copy of a
request after a short delay if the first hasn't returned; whichever finishes first
wins and the rest are cancelled. For Kinora this cuts the *p99* of cheap, safe-to-
retry calls (chat/VL/image-gen probes) without doubling load — the second copy
only ever launches when the first is already slow.

Strict safety rules baked in (so a hedge can never double-spend or corrupt):

* **Opt-in per call.** The gateway only hedges operations the caller marks
  idempotent + cheap; a Wan video render is *never* hedged (it would double the
  scarce video-budget). The executor itself is generic — the *policy* (what to
  hedge) lives in the gateway.
* **Bounded fan-out.** ``max_attempts`` caps total in-flight copies.
* **Cancellation of losers.** As soon as one copy succeeds, the rest are cancelled
  and awaited (so no orphan tasks leak), and their results/exceptions discarded.
* **First *success* wins, not first *completion*.** A fast failure does not abort
  a slower-but-succeeding copy; the executor keeps the field alive until a success
  or until every copy has failed (then it raises the last error).

Time is injected (``sleep``) so the staggering is testable without real waits.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from app.core.logging import get_logger

logger = get_logger("app.providers.resilience.hedging")

R = TypeVar("R")

#: Injectable async sleep (so tests stagger without real time).
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class HedgePolicy:
    """Tunables for :class:`HedgedExecutor`."""

    #: Total request copies allowed (1 = no hedging). The first launches at t=0,
    #: each subsequent copy after another ``delay_s``.
    max_attempts: int = 2
    #: Delay before launching each extra copy (the hedge trigger).
    delay_s: float = 0.75

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.delay_s < 0:
            raise ValueError("delay_s must be >= 0")


@dataclass
class HedgeStats:
    """Counters for hedge behavior (telemetry + tests)."""

    calls: int = 0
    hedges_launched: int = 0
    hedge_wins: int = 0


class HedgedExecutor:
    """Run an idempotent attempt with staggered duplicate copies, first-success-wins.

    The ``attempt`` factory is called once per copy with the 0-based copy index, so
    a caller can vary per-copy behavior (e.g. tag telemetry) if it wishes; for the
    common case it ignores the index.
    """

    def __init__(self, policy: HedgePolicy | None = None, *, sleep: Sleep | None = None) -> None:
        self.policy = policy or HedgePolicy()
        self._sleep = sleep or asyncio.sleep
        self.stats = HedgeStats()

    async def run(self, attempt: Callable[[int], Awaitable[R]]) -> R:
        """Execute ``attempt`` with hedging; return the first successful result.

        Raises the last exception only if *every* launched copy fails.
        """
        self.stats.calls += 1
        if self.policy.max_attempts == 1:
            return await attempt(0)

        tasks: list[asyncio.Task[R]] = []
        pending: set[asyncio.Task[R]] = set()
        launched = 0
        last_error: BaseException | None = None

        async def launch(idx: int) -> None:
            nonlocal launched
            task = asyncio.ensure_future(attempt(idx))
            tasks.append(task)
            pending.add(task)
            launched += 1
            if idx > 0:
                self.stats.hedges_launched += 1

        try:
            await launch(0)
            # Continue while copies are in flight *or* we still have hedges to launch.
            while pending or launched < self.policy.max_attempts:
                if not pending:
                    # No copy in flight but the budget allows another -> launch it.
                    await launch(launched)
                    continue
                # Wait for either a completion or the hedge timer (if copies remain).
                timeout = self.policy.delay_s if launched < self.policy.max_attempts else None
                done, pending = await self._wait(pending, timeout)
                if not done:
                    # Hedge timer fired with nothing done -> launch the next copy.
                    await launch(launched)
                    continue
                for task in done:
                    try:
                        result = task.result()
                    except asyncio.CancelledError:  # pragma: no cover - we own cancels
                        continue
                    except BaseException as exc:  # noqa: BLE001 - keep the field alive
                        last_error = exc
                        continue
                    # First success wins.
                    won_idx = tasks.index(task)
                    if won_idx > 0:
                        self.stats.hedge_wins += 1
                    await self._cancel_all(pending)
                    logger.debug("hedge.win", copy=won_idx, launched=launched)
                    return result
                # All freshly-done copies failed; the loop re-evaluates: if the
                # budget allows, it launches the next hedge; else it exits and we
                # raise the last error below.
            assert last_error is not None
            raise last_error
        finally:
            await self._cancel_all(pending)

    async def _wait(
        self, pending: set[asyncio.Task[R]], timeout: float | None
    ) -> tuple[set[asyncio.Task[R]], set[asyncio.Task[R]]]:
        if timeout is None:
            done, still = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            return done, still
        # Race the pending tasks against a hedge timer. The timer is type-erased to
        # ``Future[Any]`` so it can share the wait set with the ``Task[R]`` copies.
        timer: asyncio.Future[Any] = asyncio.ensure_future(self._sleep(timeout))
        racers: set[asyncio.Future[Any]] = {*pending, timer}
        try:
            done_any, _still_any = await asyncio.wait(racers, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not timer.done():
                timer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timer
        real_done = {t for t in pending if t in done_any}
        real_pending = pending - real_done
        return real_done, real_pending

    @staticmethod
    async def _cancel_all(tasks: set[asyncio.Task[R]]) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


__all__ = [
    "HedgePolicy",
    "HedgeStats",
    "HedgedExecutor",
    "Sleep",
]
