"""Worker + worker-pool model — the resource the router schedules over.

A :class:`Worker` is one inference engine with a finite **token budget** (its KV
cache, the real constraint on a continuous-batching serving engine) and a finite
**slot count** (max concurrent sequences). The router admits a micro-batch onto a
worker only if both fit. Each worker also tracks the **prefix keys currently
resident** in its KV cache, which is what KV-affinity routing keys on.

The :class:`WorkerPool` is per-model and implements the facet-C
:class:`~app.inference.router.protocols.WorkerPoolController` seam so an
autoscaler can grow/drain it without touching router internals.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from .errors import RouterConfigError
from .protocols import WorkerView
from .request import InferenceRequest


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Capacity of a single worker.

    Attributes:
        token_capacity: KV-cache token budget; the sum of in-flight requests'
            worst-case footprints may not exceed this.
        max_slots: Max concurrent sequences (a hard count cap independent of
            tokens, modelling per-sequence engine overhead).
        prefix_capacity: How many distinct prefix keys the worker remembers as
            "resident" (an LRU window approximating real KV residency).
    """

    token_capacity: int = 8192
    max_slots: int = 16
    prefix_capacity: int = 64

    def __post_init__(self) -> None:
        if self.token_capacity <= 0:
            raise RouterConfigError("token_capacity must be positive")
        if self.max_slots <= 0:
            raise RouterConfigError("max_slots must be positive")
        if self.prefix_capacity <= 0:
            raise RouterConfigError("prefix_capacity must be positive")


class Worker:
    """One serving engine: tracks live token/slot occupancy + KV prefix residency."""

    def __init__(self, worker_id: str, model: str, config: WorkerConfig | None = None) -> None:
        if not worker_id:
            raise RouterConfigError("worker_id must be non-empty")
        if not model:
            raise RouterConfigError("model must be non-empty")
        self.worker_id = worker_id
        self.model = model
        self.config = config or WorkerConfig()
        self._tokens_in_use = 0
        self._slots_in_use = 0
        self._healthy = True
        self._draining = False
        # LRU of prefix keys "resident" in this worker's KV cache.
        self._resident: OrderedDict[str, None] = OrderedDict()

    # -- capacity --------------------------------------------------------- #

    @property
    def tokens_in_use(self) -> int:
        return self._tokens_in_use

    @property
    def slots_in_use(self) -> int:
        return self._slots_in_use

    @property
    def token_headroom(self) -> int:
        return self.config.token_capacity - self._tokens_in_use

    @property
    def slot_headroom(self) -> int:
        return self.config.max_slots - self._slots_in_use

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def schedulable(self) -> bool:
        """Whether the router may place *new* work here."""
        return self._healthy and not self._draining

    @property
    def utilization(self) -> float:
        """Max of token/slot utilization (the binding constraint)."""
        tok = self._tokens_in_use / self.config.token_capacity
        slot = self._slots_in_use / self.config.max_slots
        return max(tok, slot)

    def can_admit(self, request: InferenceRequest) -> bool:
        """Whether ``request`` fits in the current headroom (and worker is up)."""
        if not self.schedulable:
            return False
        return (
            request.total_tokens <= self.token_headroom
            and self._slots_in_use < self.config.max_slots
        )

    # -- admission / completion ------------------------------------------- #

    def admit(self, request: InferenceRequest) -> None:
        """Reserve capacity for ``request`` and mark its prefix resident.

        Raises:
            RouterConfigError: if the request does not fit — callers must check
                :meth:`can_admit` first; admitting an over-budget request is a
                scheduler bug, not a runtime condition.
        """
        if not self.can_admit(request):
            raise RouterConfigError(
                f"worker {self.worker_id} cannot admit request {request.request_id}"
            )
        self._tokens_in_use += request.total_tokens
        self._slots_in_use += 1
        self.touch_prefix(request.prefix_key)

    def complete(
        self, request: InferenceRequest, *, actual_total_tokens: int | None = None
    ) -> None:
        """Release the capacity reserved by :meth:`admit`.

        ``actual_total_tokens`` releases the *actual* footprint when a generation
        finished short of its reservation; it is clamped so occupancy never goes
        negative even if a caller passes a stale/over-large value.
        """
        release = request.total_tokens if actual_total_tokens is None else actual_total_tokens
        self._tokens_in_use = max(0, self._tokens_in_use - release)
        self._slots_in_use = max(0, self._slots_in_use - 1)

    # -- KV residency ----------------------------------------------------- #

    def touch_prefix(self, prefix_key: str | None) -> None:
        """Mark a prefix as most-recently-resident (LRU bump / insert)."""
        if prefix_key is None:
            return
        if prefix_key in self._resident:
            self._resident.move_to_end(prefix_key)
        else:
            self._resident[prefix_key] = None
            while len(self._resident) > self.config.prefix_capacity:
                self._resident.popitem(last=False)

    def has_prefix(self, prefix_key: str | None) -> bool:
        """Whether ``prefix_key`` is currently resident in this worker's KV cache."""
        return prefix_key is not None and prefix_key in self._resident

    def resident_prefixes(self) -> tuple[str, ...]:
        """Resident prefix keys, most-recently-used last."""
        return tuple(self._resident.keys())

    # -- health / lifecycle ----------------------------------------------- #

    def set_healthy(self, healthy: bool) -> None:
        self._healthy = healthy

    def set_draining(self, draining: bool) -> None:
        self._draining = draining

    def view(self) -> WorkerView:
        """Read-only snapshot for the scaling facet."""
        return WorkerView(
            worker_id=self.worker_id,
            model=self.model,
            token_capacity=self.config.token_capacity,
            tokens_in_use=self._tokens_in_use,
            slots_capacity=self.config.max_slots,
            slots_in_use=self._slots_in_use,
            healthy=self._healthy and not self._draining,
        )


class WorkerPool:
    """A per-model set of workers; implements the facet-C controller seam."""

    def __init__(self, model: str, workers: list[Worker] | None = None) -> None:
        if not model:
            raise RouterConfigError("model must be non-empty")
        self.model = model
        self._workers: dict[str, Worker] = {}
        for worker in workers or []:
            self._register(worker)

    def _register(self, worker: Worker) -> None:
        if worker.model != self.model:
            raise RouterConfigError(
                f"worker {worker.worker_id} serves {worker.model!r}, pool is {self.model!r}"
            )
        if worker.worker_id in self._workers:
            raise RouterConfigError(f"duplicate worker id {worker.worker_id}")
        self._workers[worker.worker_id] = worker

    # -- access ----------------------------------------------------------- #

    def get(self, worker_id: str) -> Worker | None:
        return self._workers.get(worker_id)

    def workers(self) -> list[Worker]:
        """All workers (any state)."""
        return list(self._workers.values())

    def schedulable_workers(self) -> list[Worker]:
        """Workers that may receive new work (healthy + not draining)."""
        return [w for w in self._workers.values() if w.schedulable]

    @property
    def total_token_capacity(self) -> int:
        return sum(w.config.token_capacity for w in self._workers.values() if w.schedulable)

    @property
    def total_tokens_in_use(self) -> int:
        return sum(w.tokens_in_use for w in self._workers.values())

    @property
    def utilization(self) -> float:
        cap = self.total_token_capacity
        if cap <= 0:
            return 1.0
        return self.total_tokens_in_use / cap

    # -- WorkerPoolController --------------------------------------------- #

    def snapshot(self) -> list[WorkerView]:
        return [w.view() for w in self._workers.values()]

    def add_worker(self, worker_id: str) -> None:
        """Add a default-capacity worker (used by the autoscaler facet)."""
        self._register(Worker(worker_id, self.model))

    def add_configured_worker(self, worker_id: str, config: WorkerConfig) -> Worker:
        """Add a worker with explicit capacity; returns it for direct wiring."""
        worker = Worker(worker_id, self.model, config)
        self._register(worker)
        return worker

    def drain_worker(self, worker_id: str) -> None:
        worker = self._workers.get(worker_id)
        if worker is not None:
            worker.set_draining(True)

    def remove_worker(self, worker_id: str) -> None:
        """Hard-remove a fully-drained worker (no in-flight work expected)."""
        self._workers.pop(worker_id, None)


__all__ = ["Worker", "WorkerConfig", "WorkerPool"]
