"""The worker-pool state machine for the simulator (kinora.md §12.2, §instances).

A backend's capacity is a set of *workers*, each on some :class:`InstanceType`,
each moving through a lifecycle the cost model and the simulator both care about:

```
        launch                cold_start_s elapses
COLD  ───────────▶  WARMING ───────────────────────▶  WARM ──┐
  ▲                                                    │  ▲   │ accept request
  │ reclaim / drain done                               │  │   ▼
  └──────────────  DRAINING ◀── drain ──── WARM/BUSY    └─ BUSY ── finish ─▶ WARM
```

* **COLD** — not provisioned, costing nothing.
* **WARMING** — provisioned, paying the per-second rate, *cannot serve yet* (the
  cold-start penalty). This is the wall-clock a returning reader feels as a stall
  if there is no warm pool to hide it.
* **WARM** — ready, idle, still paying the per-second rate (the warm-pool cost).
* **BUSY** — serving up to ``max_concurrency`` requests.
* **DRAINING** — asked to scale down; finishes in-flight work, then goes COLD.

Spot instances can be **reclaimed** out from under in-flight work at any time
(the §instances hazard), which the simulator turns into a re-queued request.

This module owns the *single-pool* mechanics — launch/drain/reclaim, the busy-slot
bookkeeping, and incremental cost accrual — as pure state transitions over an
*injected simulation clock* (a float "now", advanced by the DES, not wall time).
The discrete-event engine in :mod:`~app.inference.scaling.simulator` drives it.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import StrEnum

from app.inference.scaling.instances import BillingModel, InstanceType

__all__ = [
    "WorkerState",
    "Worker",
    "WorkerPool",
]


class WorkerState(StrEnum):
    """A worker's lifecycle phase (see the module docstring diagram)."""

    WARMING = "warming"  # provisioned, paying, not yet servable (cold start)
    WARM = "warm"  # ready + idle
    BUSY = "busy"  # serving >=1 request
    DRAINING = "draining"  # finishing in-flight, then goes away


@dataclass
class Worker:
    """One provisioned instance and its serving state.

    ``ready_at`` is the sim-time the worker finishes cold-start (becomes WARM).
    ``inflight`` is the count of requests currently executing on it (≤ the
    instance's ``max_concurrency``). ``provisioned_at`` anchors cost accrual.
    """

    worker_id: int
    instance: InstanceType
    provisioned_at: float
    ready_at: float
    state: WorkerState = WorkerState.WARMING
    inflight: int = 0
    #: Accumulated provisioned cost charged up to ``_cost_clock`` (sim-time).
    accrued_cost: float = 0.0
    #: Of ``accrued_cost``, the portion paid while WARMING (the cold-start tax).
    accrued_cold_start_cost: float = 0.0
    #: Of ``accrued_cost``, the portion paid while WARM-and-idle (warm-pool tax).
    accrued_idle_cost: float = 0.0
    _cost_clock: float = field(default=0.0)

    def __post_init__(self) -> None:
        self._cost_clock = self.provisioned_at

    @property
    def is_servable(self) -> bool:
        """True when the worker can accept a new request right now."""
        return (
            self.state in (WorkerState.WARM, WorkerState.BUSY)
            and self.inflight < self.instance.max_concurrency
        )

    @property
    def free_slots(self) -> int:
        """Number of additional requests this worker can take on now."""
        if self.state not in (WorkerState.WARM, WorkerState.BUSY):
            return 0
        return self.instance.max_concurrency - self.inflight

    def accrue_to(self, now: float) -> None:
        """Charge provisioned cost from the last cost-clock to ``now`` (sim-time).

        Per-second billing charges for *every* second the worker exists (warming,
        warm-idle, busy); per-request-second billing charges only for busy slots.
        We attribute the warming slice to cold-start cost and the warm-idle slice
        to idle (warm-pool) cost so the report can break them out.
        """
        if now <= self._cost_clock:
            return
        dt = now - self._cost_clock
        rate = self.instance.cost_per_second
        if self.instance.billing is BillingModel.PER_SECOND:
            charge = rate * dt
            self.accrued_cost += charge
            if self.state is WorkerState.WARMING:
                self.accrued_cold_start_cost += charge
            elif self.state is WorkerState.WARM and self.inflight == 0:
                self.accrued_idle_cost += charge
        else:  # PER_REQUEST_SECOND: only busy slots cost.
            busy_fraction = self.inflight / self.instance.max_concurrency
            self.accrued_cost += rate * dt * busy_fraction
        self._cost_clock = now

    def become_warm(self, now: float) -> None:
        """Transition WARMING → WARM at its ``ready_at`` (charge the warming slice)."""
        self.accrue_to(now)
        if self.state is WorkerState.WARMING:
            self.state = WorkerState.WARM

    def start_request(self, now: float) -> None:
        """Accept one request onto a free slot (charge, then bump inflight)."""
        if not self.is_servable:
            raise RuntimeError("worker cannot accept a request in its current state")
        self.accrue_to(now)
        self.inflight += 1
        self.state = WorkerState.BUSY

    def finish_request(self, now: float) -> None:
        """Complete one in-flight request (charge, then drop inflight)."""
        if self.inflight <= 0:
            raise RuntimeError("no in-flight request to finish")
        self.accrue_to(now)
        self.inflight -= 1
        if self.inflight == 0 and self.state is WorkerState.BUSY:
            self.state = WorkerState.WARM


@dataclass
class WorkerPool:
    """A backend's elastic set of heterogeneous workers (the simulator's capacity).

    The pool tracks workers by id and exposes the mechanics the DES needs:
    launch (start a cold-start), pick a servable worker (least-loaded first to
    spread load), drain/terminate, and reclaim a spot worker. Cost is accrued
    lazily per worker; :meth:`total_cost` rolls it up at a sim-time snapshot.
    """

    workers: dict[int, Worker] = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    # ------------------------------------------------------------------ #
    # Provisioning
    # ------------------------------------------------------------------ #

    def launch(self, *, instance: InstanceType, now: float) -> Worker:
        """Provision a new worker; it becomes servable after ``cold_start_s``."""
        wid = next(self._ids)
        worker = Worker(
            worker_id=wid,
            instance=instance,
            provisioned_at=now,
            ready_at=now + instance.cold_start_s,
        )
        self.workers[wid] = worker
        return worker

    def terminate(self, worker_id: int, *, now: float) -> None:
        """Charge to ``now`` and remove a worker (drain complete / reclaim)."""
        worker = self.workers.get(worker_id)
        if worker is None:
            return
        worker.accrue_to(now)
        del self.workers[worker_id]

    # ------------------------------------------------------------------ #
    # Lifecycle ticks (driven by the DES at the relevant event times)
    # ------------------------------------------------------------------ #

    def promote_ready(self, now: float) -> list[Worker]:
        """Move any WARMING workers whose ``ready_at`` has passed to WARM."""
        promoted: list[Worker] = []
        for w in self.workers.values():
            if w.state is WorkerState.WARMING and now >= w.ready_at:
                w.become_warm(now)
                promoted.append(w)
        return promoted

    # ------------------------------------------------------------------ #
    # Scheduling
    # ------------------------------------------------------------------ #

    def pick_servable(self) -> Worker | None:
        """The least-loaded servable worker (spread load; ``None`` if all busy).

        Prefers WARM (idle) workers, then the BUSY worker with the most free slots,
        so concurrency is filled evenly and idle workers are activated first.
        """
        best: Worker | None = None
        for w in self.workers.values():
            if not w.is_servable:
                continue
            if best is None or w.free_slots > best.free_slots:
                best = w
        return best

    # ------------------------------------------------------------------ #
    # Counts + cost
    # ------------------------------------------------------------------ #

    @property
    def warm_count(self) -> int:
        """Workers able to serve (WARM or BUSY)."""
        servable = (WorkerState.WARM, WorkerState.BUSY)
        return sum(1 for w in self.workers.values() if w.state in servable)

    @property
    def warming_count(self) -> int:
        """Workers paying cold-start, not yet servable."""
        return sum(1 for w in self.workers.values() if w.state is WorkerState.WARMING)

    @property
    def total_workers(self) -> int:
        return len(self.workers)

    @property
    def free_slots(self) -> int:
        """Total servable free slots across the pool right now."""
        return sum(w.free_slots for w in self.workers.values())

    @property
    def inflight(self) -> int:
        """Total in-flight requests across the pool."""
        return sum(w.inflight for w in self.workers.values())

    def accrue_all(self, now: float) -> None:
        """Charge every worker's provisioned cost up to ``now`` (snapshot helper)."""
        for w in self.workers.values():
            w.accrue_to(now)

    def total_cost(self, now: float) -> float:
        """Total provisioned cost across the pool at sim-time ``now``."""
        self.accrue_all(now)
        return sum(w.accrued_cost for w in self.workers.values())

    def cost_by_instance_type(self, now: float) -> dict[str, float]:
        """Provisioned cost grouped by instance-type name at ``now``."""
        self.accrue_all(now)
        out: dict[str, float] = {}
        for w in self.workers.values():
            out[w.instance.name] = out.get(w.instance.name, 0.0) + w.accrued_cost
        return out

    def cold_start_cost(self, now: float) -> float:
        """Total cost paid purely while WARMING (the scale-to-zero penalty)."""
        self.accrue_all(now)
        return sum(w.accrued_cold_start_cost for w in self.workers.values())

    def idle_cost(self, now: float) -> float:
        """Total cost paid for WARM-and-idle capacity (the warm-pool tax)."""
        self.accrue_all(now)
        return sum(w.accrued_idle_cost for w in self.workers.values())
