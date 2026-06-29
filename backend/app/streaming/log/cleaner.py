"""The background log cleaner — a periodic retention/compaction driver.

:class:`LogCleaner` runs :meth:`Broker.maintain` on a fixed interval as an
``asyncio`` task, so retention (age/size eviction) and compaction (keep-latest-
per-key) happen without an explicit call from the hot path. This is the streaming
analogue of Kafka's ``LogCleaner`` thread.

It is deliberately tiny and dependency-free: it depends only on the
:class:`~app.streaming.log.broker.Broker` protocol (so it drives either broker),
takes an injectable clock + sleep for deterministic tests, and records simple
run statistics (sweeps, records removed, last error) for observability. The
``api`` process can start one per broker in its lifespan; tests drive
:meth:`sweep_once` directly without ever starting the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.streaming.log.broker import Broker

__all__ = ["CleanerStats", "LogCleaner"]


@dataclass(slots=True)
class CleanerStats:
    """Cumulative cleaner activity, surfaced for observability."""

    sweeps: int = 0
    records_removed: int = 0
    errors: int = 0
    last_error: str | None = None
    last_removed: int = 0


class LogCleaner:
    """Periodically drives :meth:`Broker.maintain` in the background."""

    def __init__(
        self,
        broker: Broker,
        *,
        interval_s: float = 30.0,
        clock: Callable[[], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._broker = broker
        self._interval_s = interval_s
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.stats = CleanerStats()

    @property
    def running(self) -> bool:
        """Whether the background loop is active."""
        return self._task is not None and not self._task.done()

    async def sweep_once(self) -> int:
        """Run a single maintenance pass; record stats; never raise.

        Returns the number of records removed (0 on error). Errors are counted
        and stashed in :attr:`stats` so a transient broker failure doesn't kill
        the loop — the next interval retries.
        """
        now = self._clock() if self._clock is not None else None
        try:
            removed = await self._broker.maintain(now=now)
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bad sweep
            self.stats.errors += 1
            self.stats.last_error = repr(exc)
            self.stats.last_removed = 0
            return 0
        self.stats.sweeps += 1
        self.stats.records_removed += removed
        self.stats.last_removed = removed
        self.stats.last_error = None
        return removed

    async def _run(self) -> None:
        while not self._stopping.is_set():
            await self.sweep_once()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue  # interval elapsed → next sweep

    def start(self) -> None:
        """Launch the background sweep loop (idempotent)."""
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Signal the loop to stop and await its exit (idempotent)."""
        self._stopping.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def __aenter__(self) -> LogCleaner:
        self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()
