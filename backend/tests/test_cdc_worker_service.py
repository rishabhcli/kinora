"""Tests for the CDC worker loop + read service (no infra)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from app.streaming.cdc.metrics import CdcMetrics, MeteredSink
from app.streaming.cdc.offsets import InMemoryOffsetStore
from app.streaming.cdc.pipeline import CDCPipeline
from app.streaming.cdc.runner import build_kinora_views
from app.streaming.cdc.service import ViewNotFoundError, ViewReadService
from app.streaming.cdc.sink import FanoutSink
from app.streaming.cdc.source import FakeChangeStream
from app.streaming.cdc.views import LibraryShelfView, MaterializedViewEngine
from app.streaming.cdc.worker import run_cdc_cycle, run_worker_loop


# --------------------------------------------------------------------------- #
# Worker cycle / loop
# --------------------------------------------------------------------------- #
async def test_run_cdc_cycle_drains_into_views() -> None:
    src = FakeChangeStream()
    src.seed_snapshot("books", [{"id": "b1", "title": "A", "status": "ready"}])
    src.push_insert("shots", {"id": "s1", "book_id": "b1", "status": "accepted"})

    engine = build_kinora_views()
    metrics = CdcMetrics()
    sink = MeteredSink(FanoutSink([engine]), metrics)
    pipeline = CDCPipeline(connector="k", source=src, sink=sink, offsets=InMemoryOffsetStore())

    status = await run_cdc_cycle(pipeline=pipeline, engine=engine, metrics=metrics)
    assert status["views"]["library_shelf"] == 1
    assert status["views"]["shots_per_book"] == 1
    assert status["delivered"] >= 2


async def test_cycles_resume_across_runs_no_duplicates() -> None:
    # Deterministic cross-cycle resume: cycle 1 sees b1; a new row arrives; cycle
    # 2 picks up only b2 (resumes after the committed offset, no re-delivery).
    src = FakeChangeStream()
    offsets = InMemoryOffsetStore()
    engine = build_kinora_views()
    metrics = CdcMetrics()
    sink = MeteredSink(FanoutSink([engine]), metrics)

    def make_pipeline() -> CDCPipeline:
        return CDCPipeline(connector="k", source=src, sink=sink, offsets=offsets)

    src.push_insert("books", {"id": "b1", "title": "A", "status": "ready"})
    s1 = await run_cdc_cycle(pipeline=make_pipeline(), engine=engine, metrics=metrics)
    assert s1["delivered"] == 1

    src.push_insert("books", {"id": "b2", "title": "B", "status": "ready"})
    s2 = await run_cdc_cycle(pipeline=make_pipeline(), engine=engine, metrics=metrics)
    assert s2["delivered"] == 1  # only b2, b1 not re-delivered

    shelf = {r["book_id"] for r in engine.rows("library_shelf")}
    assert shelf == {"b1", "b2"}


async def test_worker_loop_stops_cleanly() -> None:
    # The loop terminates promptly when stop is pre-set (no work, no hang).
    engine = build_kinora_views()
    metrics = CdcMetrics()
    stop = asyncio.Event()
    stop.set()

    def make_pipeline() -> CDCPipeline:
        src = FakeChangeStream()
        return CDCPipeline(
            connector="k",
            source=src,
            sink=MeteredSink(FanoutSink([engine]), metrics),
            offsets=InMemoryOffsetStore(),
        )

    await asyncio.wait_for(
        run_worker_loop(
            make_pipeline=make_pipeline,
            engine=engine,
            metrics=metrics,
            poll_interval_s=0.0,
            stop=stop,
        ),
        timeout=1.0,
    )


async def test_worker_loop_survives_cycle_error() -> None:
    engine = build_kinora_views()
    metrics = CdcMetrics()
    stop = asyncio.Event()
    calls = {"n": 0}

    def make_pipeline() -> CDCPipeline:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        # Second cycle: a working pipeline that stops the loop.
        src = FakeChangeStream()
        src.push_insert("books", {"id": "b1", "title": "A", "status": "ready"})
        stop.set()
        return CDCPipeline(
            connector="k",
            source=src,
            sink=MeteredSink(FanoutSink([engine]), metrics),
            offsets=InMemoryOffsetStore(),
        )

    await run_worker_loop(
        make_pipeline=make_pipeline,
        engine=engine,
        metrics=metrics,
        poll_interval_s=0.0,
        stop=stop,
    )
    assert calls["n"] >= 2
    assert metrics.snapshot()["errors"] >= 1  # the transient was recorded
    assert len(engine.rows("library_shelf")) == 1


# --------------------------------------------------------------------------- #
# Read service
# --------------------------------------------------------------------------- #
async def test_read_service_over_live_engine() -> None:
    engine = MaterializedViewEngine()
    engine.register(LibraryShelfView())
    from app.streaming.cdc.events import ChangeEvent, LogPosition

    engine.apply(
        ChangeEvent.insert(
            "books",
            {"id": "b1", "title": "A", "status": "ready", "user_id": "u1"},
            LogPosition(1, 0),
        )
    )
    engine.apply(
        ChangeEvent.insert(
            "books",
            {"id": "b2", "title": "B", "status": "ready", "user_id": "u2"},
            LogPosition(2, 0),
        )
    )

    svc = ViewReadService(engine)
    assert svc.views == {"library_shelf"}
    assert await svc.count("library_shelf") == 2
    mine = await svc.read("library_shelf", where={"owner_id": "u1"})
    assert [r["book_id"] for r in mine] == ["b1"]
    limited = await svc.read("library_shelf", limit=1)
    assert len(limited) == 1


async def test_read_service_unknown_view_raises() -> None:
    svc = ViewReadService(MaterializedViewEngine())
    with pytest.raises(ViewNotFoundError):
        await svc.read("nope")


async def test_read_service_checkpoint_fallback() -> None:
    # No live engine for this view → fall back to a fake checkpoint store.
    class _FakeCheckpoint:
        async def rows(self, view_name: str) -> list[Mapping[str, Any]]:
            if view_name == "library_shelf":
                return [{"book_id": "b1", "title": "A"}]
            return []

    svc = ViewReadService(engine=None, checkpoint=_FakeCheckpoint())  # type: ignore[arg-type]
    rows = await svc.read("library_shelf")
    assert rows == [{"book_id": "b1", "title": "A"}]
