"""Long-running product-analytics rollup worker.

Mirrors the ingest-recovery worker's process model (``python -m
app.analytics.rollup_worker``): periodically re-aggregate the trailing event
window into the summary tables (``analytics_daily_rollup`` /
``analytics_sessions``). Re-aggregating a trailing window each tick keeps
late-arriving events correct; the idempotent upserts make the overlap harmless.

A cloud deployment can run this as a separate ECS role; locally it can be run
on demand. It is a *consumer* of the analytics event log, never on the request
hot path.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.composition import build_container
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("app.analytics.rollup_worker")


async def run_rollup_loop(stop: asyncio.Event | None = None) -> None:
    """Re-aggregate the trailing window on a cadence until ``stop`` is set."""
    settings = get_settings()
    container = build_container(settings)
    stop = stop or asyncio.Event()
    try:
        while not stop.is_set():
            if settings.analytics_enabled:
                try:
                    counts = await container.run_analytics_rollup()
                    logger.info("analytics.rollup.tick", **counts)
                except Exception as exc:  # noqa: BLE001 - a tick failure must not kill the loop
                    logger.warning("analytics.rollup.tick_failed", error=str(exc))
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.analytics_rollup_interval_s
                )
            except TimeoutError:
                continue
    finally:
        await container.shutdown()


def main() -> int:
    """``python -m app.analytics.rollup_worker`` entrypoint."""
    settings = get_settings()
    configure_logging(settings.log_level)

    async def _run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await run_rollup_loop(stop)

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
