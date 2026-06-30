"""Distributed render orchestration across many workers and providers (kinora.md §12.1/§12.2).

Today: a single Redis priority queue (:mod:`app.queue`) drained by one render
worker; the API runs the scheduler in-process. At scale Kinora runs *many* render
workers across providers (Wan / MiniMax / local) on ECS / Function-Compute, and
something must decide which worker renders which shot — without ever rendering a
shot twice or stranding one behind a crashed box.

This package is that coordination layer, built *beside* the queue rather than
inside it. It is pure coordination over injectable seams (an
:class:`~app.orchestration.store.OrchestrationStore`, a
:class:`~app.orchestration.capacity.CapacityOracle`, a
:class:`~app.orchestration.clock.Clock`), so the whole subsystem runs
deterministically with an in-memory store + a virtual clock + fake workers and
zero infra.

Pieces:

* :mod:`models` — the noun layer (lanes, capabilities, tickets, leases + fence).
* :mod:`store` — the registry/lease state seam + an in-memory impl (fence CAS).
* :mod:`capacity` — the provider-capacity oracle seam (governor-style headroom).
* :mod:`registry` — worker register / heartbeat / drain + dead-worker sweep.
* :mod:`placement` — pure capability + capacity + locality placement policy.
* :mod:`coordinator` — assignment, exactly-once leases, crash reassignment.
* :mod:`rebalance` — pure work-stealing planner (idle ↔ backed-up).
* :mod:`progress` — the global fleet progress / lag projection.
* :mod:`service` — the wired façade + one-pass control-loop ``tick``.
"""

from __future__ import annotations

from app.orchestration.capacity import (
    CapacityOracle,
    ProviderCapacity,
    StaticCapacityOracle,
)
from app.orchestration.clock import Clock, MonotonicClock, VirtualClock
from app.orchestration.coordinator import (
    Assignment,
    AssignmentBatch,
    RenderCoordinator,
)
from app.orchestration.models import (
    FenceViolationError,
    Lane,
    LeaseError,
    ShotLease,
    ShotTicket,
    WorkerCapabilities,
    WorkerDescriptor,
    WorkerStatus,
)
from app.orchestration.placement import WorkerLoad, choose_worker, score_candidates
from app.orchestration.progress import FleetProgress, build_progress
from app.orchestration.rebalance import (
    Migration,
    RebalanceConfig,
    Rebalancer,
    StealPlan,
)
from app.orchestration.registry import RegistryConfig, SweepReport, WorkerRegistry
from app.orchestration.service import (
    OrchestrationService,
    TickReport,
    build_orchestration_service,
)
from app.orchestration.store import InMemoryOrchestrationStore, OrchestrationStore

__all__ = [
    # models
    "Lane",
    "LeaseError",
    "FenceViolationError",
    "WorkerStatus",
    "WorkerCapabilities",
    "WorkerDescriptor",
    "ShotTicket",
    "ShotLease",
    # clock
    "Clock",
    "MonotonicClock",
    "VirtualClock",
    # store
    "OrchestrationStore",
    "InMemoryOrchestrationStore",
    # capacity
    "CapacityOracle",
    "ProviderCapacity",
    "StaticCapacityOracle",
    # registry
    "WorkerRegistry",
    "RegistryConfig",
    "SweepReport",
    # placement
    "WorkerLoad",
    "choose_worker",
    "score_candidates",
    # coordinator
    "RenderCoordinator",
    "Assignment",
    "AssignmentBatch",
    # rebalance
    "Rebalancer",
    "RebalanceConfig",
    "Migration",
    "StealPlan",
    # progress
    "FleetProgress",
    "build_progress",
    # service
    "OrchestrationService",
    "TickReport",
    "build_orchestration_service",
]
