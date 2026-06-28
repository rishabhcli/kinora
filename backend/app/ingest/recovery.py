"""Long-running ingest recovery worker.

The API also kicks this once at startup, but cloud deployments can run this as a
separate ECS role so interrupted uploads/imports recover even when API instances
are focused on serving traffic.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.composition import build_container
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("app.ingest.recovery")


async def run_recovery_loop(stop: asyncio.Event | None = None) -> None:
    """Scan for stuck importing books until ``stop`` is set."""
    settings = get_settings()
    container = build_container(settings)
    stop = stop or asyncio.Event()
    try:
        while not stop.is_set():
            recovered = await container.recover_importing_books(
                limit=settings.ingest_recovery_limit
            )
            logger.info("ingest.recovery.tick", recovered=recovered)
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.ingest_recovery_interval_s)
            except TimeoutError:
                continue
    finally:
        await container.shutdown()


def main() -> int:
    """``python -m app.ingest.recovery`` entrypoint."""
    settings = get_settings()
    configure_logging(settings.log_level)

    async def _run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await run_recovery_loop(stop)

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
