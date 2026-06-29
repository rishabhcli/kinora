"""Composition seam — assemble a ready saga engine over chosen backends.

A single factory the rest of the backend (or a worker entrypoint) calls to get a
wired :class:`SagaEngine`: a registry pre-loaded with the concrete Kinora flows, a
:class:`SagaOrchestrator` over the chosen store + effect ledger, and a
:class:`SagaWorker` to drain it. Defaults to the **in-memory** backends (so it is
usable with zero infrastructure — tests, the harness, a dev loop); pass a
committing ``session_factory`` to switch to the durable Postgres store + ledger,
and/or a Redis handle to use the cross-process effect ledger / lock manager.

This is the saga analogue of :func:`app.composition.build_container` for the rest
of the system: lazy, dependency-light, and side-effect-free to import. It lives in
this package (additive) rather than mutating the root composition root.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.distributed.sagas.definition import SagaRegistry
from app.distributed.sagas.effects import (
    EffectLedger,
    InMemoryEffectLedger,
    RedisEffectLedger,
)
from app.distributed.sagas.flows.ingest import build_ingest_saga
from app.distributed.sagas.flows.render import build_render_saga
from app.distributed.sagas.locks import (
    InMemoryLockManager,
    LockManager,
    RedisLockManager,
)
from app.distributed.sagas.orchestrator import SagaOrchestrator
from app.distributed.sagas.runner import SagaWorker
from app.distributed.sagas.store import InMemorySagaStore, SagaStore
from app.jobs.clock import Clock, SystemClock


@dataclass(slots=True)
class SagaEngine:
    """A fully wired saga engine: registry + store + orchestrator + worker + locks."""

    registry: SagaRegistry
    store: SagaStore
    effects: EffectLedger
    locks: LockManager
    orchestrator: SagaOrchestrator
    worker: SagaWorker


def default_registry() -> SagaRegistry:
    """A registry pre-loaded with the two concrete Kinora flows."""
    reg = SagaRegistry()
    reg.register(build_ingest_saga())
    reg.register(build_render_saga())
    return reg


def build_saga_engine(
    *,
    registry: SagaRegistry | None = None,
    session_factory: Any | None = None,
    redis: Any | None = None,
    resources: Mapping[str, Any] | None = None,
    clock: Clock | None = None,
    lease_seconds: float = 60.0,
) -> SagaEngine:
    """Assemble a :class:`SagaEngine` over the chosen backends.

    Args:
        registry: the saga definitions to drive (defaults to the Kinora flows).
        session_factory: a committing async-session factory (the shape of
            :func:`app.db.session.get_session`); when given, the durable Postgres
            store + effect ledger are used. When ``None``, the in-memory backends
            are used (zero infra).
        redis: a Redis handle; when given (and no ``session_factory``), the
            cross-process Redis effect ledger is used, and the Redis lock manager is
            always preferred for distributed locks when a handle is present.
        resources: the DI bag injected into every step (e.g. ``{"ingest_ports":
            ..., "render_ports": ...}`` — the production service adapters).
        clock: the time source (defaults to :class:`SystemClock`).
        lease_seconds: how long the worker leases a claimed instance while driving.
    """
    reg = registry or default_registry()
    the_clock = clock or SystemClock()

    store: SagaStore
    effects: EffectLedger
    if session_factory is not None:
        from app.distributed.sagas.db_store import (
            PostgresEffectLedger,
            PostgresSagaStore,
        )

        store = PostgresSagaStore(session_factory)
        effects = PostgresEffectLedger(session_factory)
    else:
        store = InMemorySagaStore()
        effects = (
            RedisEffectLedger(redis, clock=the_clock)
            if redis is not None
            else InMemoryEffectLedger(clock=the_clock)
        )

    locks: LockManager = (
        RedisLockManager(redis, clock=the_clock)
        if redis is not None
        else InMemoryLockManager(clock=the_clock)
    )

    orchestrator = SagaOrchestrator(
        store, reg, clock=the_clock, effects=effects, resources=resources
    )
    worker = SagaWorker(store, orchestrator, clock=the_clock, lease_seconds=lease_seconds)
    return SagaEngine(
        registry=reg,
        store=store,
        effects=effects,
        locks=locks,
        orchestrator=orchestrator,
        worker=worker,
    )


__all__ = ["SagaEngine", "build_saga_engine", "default_registry"]
