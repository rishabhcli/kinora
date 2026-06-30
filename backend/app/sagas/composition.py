"""Lightweight composition helpers for the saga engine.

Bundles the engine + store + registry + recovery sweeper into one
:class:`SagaRuntime` so a caller (the production composition root or a test)
wires the subsystem in a single call. Settings are read additively from
:class:`app.core.config.Settings`; nothing here requires infra or network, so it
is safe to construct under ``DASHSCOPE_API_KEY=test``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.sagas.clock import SYSTEM_CLOCK, Clock
from app.sagas.definition import Workflow
from app.sagas.engine import RunIdFactory, SagaEngine, Sleeper, _real_sleep
from app.sagas.ids import new_run_id
from app.sagas.recovery import RecoverySweeper
from app.sagas.registry import WorkflowRegistry
from app.sagas.store import DurableStore, InMemoryDurableStore
from app.sagas.telemetry import TelemetryBus


@dataclass(slots=True)
class SagaRuntime:
    """A fully-wired saga subsystem: registry + store + engine + sweeper."""

    registry: WorkflowRegistry
    store: DurableStore
    engine: SagaEngine
    sweeper: RecoverySweeper
    bus: TelemetryBus


def build_saga_runtime(
    workflows: list[Workflow] | None = None,
    *,
    store: DurableStore | None = None,
    clock: Clock = SYSTEM_CLOCK,
    sleeper: Sleeper = _real_sleep,
    bus: TelemetryBus | None = None,
    run_id_factory: RunIdFactory = new_run_id,
    lease_ttl_s: float = 300.0,
    owner: str = "engine",
) -> SagaRuntime:
    """Wire a :class:`SagaRuntime`.

    Defaults to an :class:`~app.sagas.store.InMemoryDurableStore` and the system
    clock; production passes a DB/Redis-backed store. The same ``clock`` + ``bus``
    are shared by the engine and the sweeper so timer firing stays coherent.
    """
    registry = WorkflowRegistry(workflows)
    used_store: DurableStore = store or InMemoryDurableStore()
    used_bus = bus or TelemetryBus()
    engine = SagaEngine(
        registry,
        used_store,
        clock=clock,
        sleeper=sleeper,
        bus=used_bus,
        run_id_factory=run_id_factory,
        lease_ttl_s=lease_ttl_s,
        owner=owner,
    )
    sweeper = RecoverySweeper(engine, used_store, clock=clock, bus=used_bus)
    return SagaRuntime(
        registry=registry,
        store=used_store,
        engine=engine,
        sweeper=sweeper,
        bus=used_bus,
    )


__all__ = ["SagaRuntime", "build_saga_runtime"]
