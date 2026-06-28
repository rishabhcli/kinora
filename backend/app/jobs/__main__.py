"""Run the jobs framework as a standalone process: ``python -m app.jobs``.

A thin entrypoint that mirrors the other Kinora workers (``app.queue.worker``,
``app.ingest.recovery``): build a :class:`~app.jobs.service.JobService` wired with
the built-in maintenance jobs over the configured Redis + Postgres, start its
scheduler + worker loops under a distributed leader lease, and run until a signal
arrives. It is **opt-in** — nothing in the FastAPI app starts this — so the
framework only runs where an operator deploys this command (a dedicated
``jobs-worker`` service), keeping it out of the shared composition root.

The maintenance jobs' target subsystems are not injected here (this minimal
entrypoint leaves the resource bag empty), so every built-in job *skips cleanly*:
the loop is exercised end-to-end without forcing any subsystem to exist. A richer
deployment would inject ``digest_flusher`` / ``search_indexer`` / ``retention_gc``
/ ``import_recovery`` / ``budget_reconciler`` resources to do real work.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.jobs.maintenance import default_maintenance_registry
from app.jobs.service import build_job_service

logger = get_logger("app.jobs.main")


async def run() -> None:
    """Build and run the job service until SIGINT/SIGTERM."""
    settings = get_settings()
    configure_logging(settings.log_level)

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.composition import make_session_factory
    from app.redis.client import RedisClient

    engine = create_async_engine(settings.database_url, pool_pre_ping=True, future=True)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session_factory = make_session_factory(maker)
    redis = RedisClient.from_url(settings.redis_url)

    service = build_job_service(
        redis=redis.raw,
        session_factory=session_factory,
        registry=default_maintenance_registry(),
        enable_leader_election=True,
        store_backend="postgres",
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    service.start()
    logger.info("jobs.main.started", jobs=len(service.registry))
    try:
        await stop.wait()
    finally:
        await service.stop()
        with contextlib.suppress(Exception):
            await redis.close()
        with contextlib.suppress(Exception):
            await engine.dispose()
        logger.info("jobs.main.stopped")


def main() -> None:
    """Synchronous console entrypoint."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
