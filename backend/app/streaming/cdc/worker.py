"""The CDC worker — a long-running connector process (mirrors the §12.1 workers).

Like the render / ingest workers, this is the same backend image run with a
different command. It continuously polls the operational tables for changes,
maintains the canonical Kinora materialised views (``build_kinora_views``),
periodically checkpoints view state, and exposes metrics.

It is built around the **polling** source by default because that needs no
special Postgres privileges (logical replication slots require elevated rights);
a deployment with a slot swaps in :class:`PostgresLogicalSource`. The whole loop
is timer-driven by ``poll_interval_s`` and stops cleanly on SIGINT/SIGTERM.

The interesting work — capture, snapshot bootstrap, incremental maintenance — is
in the pure modules and is fully unit-tested with fakes. This entrypoint only
wires real infra (a session factory + the polling fetchers) and runs the loop,
so it degrades to a no-op when no database is configured (the unit suite never
needs it; integration drives :func:`run_cdc_cycle` directly with fakes).

Run it as a process with ``python -m app.streaming.cdc.worker``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Callable, Sequence
from typing import Any

from app.core.logging import configure_logging, get_logger
from app.streaming.cdc.db_adapters import (
    SqlAlchemyRowFetcher,
    ViewStateCheckpointStore,
    kinora_polled_tables,
)
from app.streaming.cdc.metrics import CdcMetrics, MeteredSink
from app.streaming.cdc.offsets import OffsetStore
from app.streaming.cdc.pipeline import CDCPipeline
from app.streaming.cdc.polling_source import PollingSource, RowFetcher
from app.streaming.cdc.runner import build_kinora_views
from app.streaming.cdc.sink import FanoutSink
from app.streaming.cdc.views.engine import MaterializedViewEngine

logger = get_logger("app.streaming.cdc.worker")

_DEFAULT_POLL_INTERVAL_S = 2.0

#: A zero-arg callable that builds a fresh pipeline over persistent source state.
PipelineBuilder = Callable[[], CDCPipeline]


async def run_cdc_cycle(
    *,
    pipeline: CDCPipeline,
    engine: MaterializedViewEngine,
    metrics: CdcMetrics,
    checkpoint: ViewStateCheckpointStore | None = None,
) -> dict[str, Any]:
    """Run one capture cycle: drain the source into the views, then report.

    For a polling source ``pipeline.run`` performs one ``poll_once`` pass per
    table (snapshot on the first cycle, incremental thereafter), applies every
    change to the engine, and checkpoints offsets. Returns a small status dict
    for logging / a health probe. Pure enough to drive deterministically from a
    test with a fake source.
    """
    result = await pipeline.run()
    if checkpoint is not None:
        for view_name in engine.graph.views:
            await checkpoint.save(engine.view(view_name))
    status = {
        "delivered": result.delivered,
        "deduped": result.deduped,
        "snapshot_rows": result.snapshot_rows,
        "views": {v: len(engine.rows(v)) for v in engine.graph.views},
        "metrics": metrics.snapshot(),
    }
    return status


def build_polling_source(
    *,
    fetcher: RowFetcher,
    tables: Sequence[str] | None = None,
) -> PollingSource:
    """A polling source over the given tables (defaults to the Kinora set)."""
    return PollingSource(fetcher, list(tables or kinora_polled_tables()))


async def run_worker_loop(
    *,
    make_pipeline: PipelineBuilder,
    engine: MaterializedViewEngine,
    metrics: CdcMetrics,
    checkpoint: ViewStateCheckpointStore | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    stop: asyncio.Event | None = None,
) -> None:
    """Poll on a cadence until ``stop`` is set.

    ``make_pipeline`` is called once per cycle to build a fresh pipeline bound to
    the persistent source/offsets (so the offset/cursor state carries across
    cycles while the per-run dedup state resets). Resilient: a cycle error is
    logged and the loop continues (the next cycle resumes from the committed
    offset).
    """
    stop = stop or asyncio.Event()
    cycle = 0
    while not stop.is_set():
        cycle += 1
        try:
            pipeline = make_pipeline()
            status = await run_cdc_cycle(
                pipeline=pipeline,
                engine=engine,
                metrics=metrics,
                checkpoint=checkpoint,
            )
            logger.info("cdc.worker.tick", cycle=cycle, **_loggable(status))
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            metrics.record_error()
            logger.warning("cdc.worker.cycle_error", cycle=cycle, error=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_interval_s)


def _loggable(status: dict[str, Any]) -> dict[str, Any]:
    """Flatten the status into structlog-friendly scalar fields."""
    return {
        "delivered": status.get("delivered"),
        "deduped": status.get("deduped"),
        "snapshot_rows": status.get("snapshot_rows"),
        "views": status.get("views"),
    }


class PipelineFactory:
    """Callable that builds a fresh :class:`CDCPipeline` over persistent state.

    Holds the long-lived source, offset store, and metered fan-out sink; produces
    a new pipeline each cycle (the pipeline's per-run dedup map resets, but the
    source cursor + offsets persist, so progress is monotonic).
    """

    def __init__(
        self,
        *,
        connector: str,
        source: PollingSource,
        engine: MaterializedViewEngine,
        offsets: OffsetStore,
        metrics: CdcMetrics,
    ) -> None:
        self._connector = connector
        self._source = source
        self._sink = MeteredSink(FanoutSink([engine]), metrics)
        self._offsets = offsets

    def __call__(self) -> CDCPipeline:
        return CDCPipeline(
            connector=self._connector,
            source=self._source,
            sink=self._sink,
            offsets=self._offsets,
        )


def _build_runtime() -> (
    tuple[PipelineFactory, MaterializedViewEngine, CdcMetrics, ViewStateCheckpointStore] | None
):
    """Wire the real-infra runtime, or ``None`` when no database is configured."""
    from app.core.config import get_settings
    from app.db.models.book import Book, Page
    from app.db.models.continuity import ContinuityState
    from app.db.models.entity import Entity
    from app.db.models.shot import Shot
    from app.streaming.cdc.offsets import InMemoryOffsetStore

    settings = get_settings()
    db_url = getattr(settings, "database_url", None)
    if not db_url:
        return None

    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    db_engine = create_async_engine(str(db_url))
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        async with maker() as session:
            yield session
            await session.commit()

    # One model per polled table (single-table fetchers fanned into the source).
    models: dict[str, type[Any]] = {
        "books": Book,
        "pages": Page,
        "entities": Entity,
        "continuity_states": ContinuityState,
        "shots": Shot,
    }
    # A multiplexing fetcher routes per-table to the right single-model fetcher.
    fetchers: dict[str, RowFetcher] = {
        t: SqlAlchemyRowFetcher(scope, m) for t, m in models.items()
    }
    source = PollingSource(_MultiFetcher(fetchers), list(models))
    engine = build_kinora_views()
    metrics = CdcMetrics()
    factory = PipelineFactory(
        connector="kinora-cdc",
        source=source,
        engine=engine,
        offsets=InMemoryOffsetStore(),
        metrics=metrics,
    )
    return factory, engine, metrics, ViewStateCheckpointStore(scope)


class _MultiFetcher(RowFetcher):
    """Routes ``fetch_*`` per table to a table-specific :class:`RowFetcher`."""

    def __init__(self, by_table: dict[str, RowFetcher]) -> None:
        self._by_table = by_table

    async def fetch_changed(self, table, *, after, limit):  # type: ignore[no-untyped-def]
        return await self._by_table[table].fetch_changed(table, after=after, limit=limit)

    async def fetch_snapshot(self, table, *, limit):  # type: ignore[no-untyped-def]
        return await self._by_table[table].fetch_snapshot(table, limit=limit)


def main() -> int:
    """``python -m app.streaming.cdc.worker`` entrypoint."""
    from app.core.config import get_settings

    settings = get_settings()
    configure_logging(getattr(settings, "log_level", "INFO"))

    runtime = _build_runtime()
    if runtime is None:
        logger.warning("cdc.worker.no_database_configured")
        return 0
    factory, engine, metrics, checkpoint = runtime

    async def _run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await run_worker_loop(
            make_pipeline=factory,
            engine=engine,
            metrics=metrics,
            checkpoint=checkpoint,
            stop=stop,
        )

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PipelineBuilder",
    "PipelineFactory",
    "build_polling_source",
    "run_cdc_cycle",
    "run_worker_loop",
]
