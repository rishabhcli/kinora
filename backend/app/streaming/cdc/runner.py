"""The CDC runner — the composition seam that assembles a working data plane.

A :class:`CDCRunner` wires the pieces into a runnable unit:

    source ──▶ MeteredSink ──▶ FanoutSink ──┬─▶ MaterializedViewEngine
                                            └─▶ (optional) Broker / Redis sink
                              CDCPipeline (snapshot bootstrap, dedup, offsets)

It also ships :func:`build_kinora_views`, the canonical projection set the
product reads — the library shelf, the canon-graph projection, shots-per-book,
and active-characters-per-book — registered on one engine. This is what a
backend service (the API process, a dedicated CDC worker) would construct.

The runner is infra-agnostic: pass a :class:`FakeChangeStream` and an
:class:`InMemoryOffsetStore` for a deterministic test; pass a
:class:`PostgresLogicalSource` (+ a polling fallback) and a
:class:`DbOffsetStore` in production. No timers, no global state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.streaming.cdc.metrics import CdcMetrics, MeteredSink
from app.streaming.cdc.offsets import InMemoryOffsetStore, OffsetStore
from app.streaming.cdc.pipeline import CDCPipeline, PipelineResult
from app.streaming.cdc.schema import SchemaRegistry
from app.streaming.cdc.sink import ChangeSink, FanoutSink
from app.streaming.cdc.source import CDCSource
from app.streaming.cdc.views import (
    AggregateView,
    CanonGraphView,
    CountReducer,
    DistinctCountReducer,
    LibraryShelfView,
    MaterializedViewEngine,
)


def build_kinora_views() -> MaterializedViewEngine:
    """The canonical Kinora read-model projection set on one engine."""
    engine = MaterializedViewEngine()
    engine.register(LibraryShelfView())
    engine.register(CanonGraphView())
    # "shots per book" — a live render-progress counter.
    engine.register(
        AggregateView(
            name="shots_per_book",
            source="shots",
            group_by=("book_id",),
            aggregates={"shot_count": CountReducer()},
        )
    )
    # "accepted shots per book" — completed render progress (§9.7 'accepted').
    engine.register(
        AggregateView(
            name="accepted_shots_per_book",
            source="shots",
            group_by=("book_id",),
            aggregates={"accepted": CountReducer()},
            where=lambda r: r.get("status") == "accepted",
        )
    )
    # "distinct active characters per book" off the canon entities.
    engine.register(
        AggregateView(
            name="characters_per_book",
            source="entities",
            group_by=("book_id",),
            aggregates={"characters": DistinctCountReducer("entity_key")},
            where=lambda r: r.get("type") == "character" and r.get("valid_to_beat") is None,
        )
    )
    return engine


@dataclass(slots=True)
class RunnerResult:
    """The outcome of a :meth:`CDCRunner.run` (pipeline result + metrics)."""

    pipeline: PipelineResult
    metrics: dict[str, Any]


class CDCRunner:
    """Assemble + run one connector: source → metered fan-out → engine (+ extras)."""

    def __init__(
        self,
        *,
        connector: str,
        source: CDCSource,
        engine: MaterializedViewEngine | None = None,
        extra_sinks: list[ChangeSink] | None = None,
        offsets: OffsetStore | None = None,
        schema_registry: SchemaRegistry | None = None,
        metrics: CdcMetrics | None = None,
        commit_every: int = 1,
        resume: bool = True,
    ) -> None:
        self.connector = connector
        self.engine = engine or build_kinora_views()
        self.metrics = metrics or CdcMetrics()
        sinks: list[ChangeSink] = [self.engine, *(extra_sinks or [])]
        fan = FanoutSink(sinks)
        metered = MeteredSink(fan, self.metrics)
        self._pipeline = CDCPipeline(
            connector=connector,
            source=source,
            sink=metered,
            offsets=offsets or InMemoryOffsetStore(),
            schema_registry=schema_registry,
            commit_every=commit_every,
            resume=resume,
        )

    @property
    def pipeline(self) -> CDCPipeline:
        return self._pipeline

    async def run(self) -> RunnerResult:
        result = await self._pipeline.run()
        return RunnerResult(pipeline=result, metrics=self.metrics.snapshot())

    def rows(self, view: str) -> list[dict[str, Any]]:
        return self.engine.rows(view)


__all__ = ["CDCRunner", "RunnerResult", "build_kinora_views"]
