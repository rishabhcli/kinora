"""A FoundationDB-style deterministic simulation framework for Kinora's control
plane (kinora.md §4 generation-on-scroll, §6 architecture, §9.7 shot lifecycle,
§12 the unglamorous engineering).

The framework runs the **real** reading→scheduler→queue→render→events loop —
:class:`~app.scheduler.service.SchedulerService` and
:class:`~app.queue.redis_queue.RedisRenderQueue` — inside a single-threaded
deterministic simulator: a virtual clock, a simulated network / disk / redis with
injectable faults, and a Buggify-style fault grammar, all seeded for perfect
reproducibility. It asserts end-to-end invariants (buffer health, no stuck shots,
eventual consistency, no double-spend) across thousands of seeded fault schedules,
with minimal-failing-seed shrinking and exact replay.

Layers (bottom → top):

* :mod:`core` — the deterministic engine: a splittable seeded PRNG, a virtual
  :class:`~core.SimClock`, and a single-threaded discrete-event
  :class:`~core.EventLoop`.
* :mod:`faults` / :mod:`buggify` — the fault grammar (:class:`~faults.FaultKind`,
  :class:`~faults.FaultProfile`, :class:`~faults.FaultSchedule`) and the
  :class:`~buggify.Buggify` injection gate that reads it.
* :mod:`network` / :mod:`storage` / :mod:`redis_sim` — the simulated seams
  (latency / reorder / drop / partition; IO errors / lost acks / stale reads;
  a fault-injecting proxy over the project's own ``FakeAsyncRedis``).
* :mod:`runtime` — :class:`~runtime.Simulation`, the bridge that drives the real
  ``async`` services to completion at each virtual instant on one owned clock.
* :mod:`workload` / :mod:`collaborators` / :mod:`events` — the seeded reader
  model, the scheduler's sim doubles, and the capturing event tap.
* :mod:`system` — the end-to-end wiring (:func:`~system.run_system`).
* :mod:`invariants` — the safety / liveness / quality properties.
* :mod:`runner` — :func:`~runner.sweep`, :func:`~runner.shrink`,
  :func:`~runner.replay`: the verification workflow.

Nothing here spends a credit or calls a provider: ``KINORA_LIVE_VIDEO`` is
irrelevant because no provider is ever invoked (the budget pool is virtual). The
whole multi-minute session simulates in milliseconds of wall-clock.
"""

from __future__ import annotations

from app.verification.simulation.buggify import Buggify, BuggifyLog
from app.verification.simulation.core import EventLoop, Prng, SimClock
from app.verification.simulation.faults import (
    FaultKind,
    FaultProfile,
    FaultSchedule,
    FaultWeight,
)
from app.verification.simulation.invariants import (
    CORE_INVARIANTS,
    STRICT_INVARIANTS,
    Invariant,
    InvariantReport,
    InvariantResult,
    check_invariants,
)
from app.verification.simulation.runner import (
    SeedResult,
    ShrinkResult,
    SweepResult,
    replay,
    run_seed,
    shrink,
    sweep,
)
from app.verification.simulation.runtime import Simulation
from app.verification.simulation.system import (
    SimulatedSystem,
    SystemConfig,
    SystemReport,
    run_system,
)

__all__ = [
    "CORE_INVARIANTS",
    "STRICT_INVARIANTS",
    "Buggify",
    "BuggifyLog",
    "EventLoop",
    "FaultKind",
    "FaultProfile",
    "FaultSchedule",
    "FaultWeight",
    "Invariant",
    "InvariantReport",
    "InvariantResult",
    "Prng",
    "SeedResult",
    "ShrinkResult",
    "SimClock",
    "SimulatedSystem",
    "Simulation",
    "SweepResult",
    "SystemConfig",
    "SystemReport",
    "check_invariants",
    "replay",
    "run_seed",
    "run_system",
    "shrink",
    "sweep",
]
