"""Shared protocols for the inference gateway — the seam between facets.

Three facets compose into one inference gateway and meet *here*:

* **Facet A — router** (this package): admission, fair-share scheduling,
  continuous-batch bin-packing, KV-affinity routing, coalescing.
* **Facet B — accel** (sibling): speculative decoding + a response/prefix cache
  that plugs in as an :class:`InferenceBackend` decorator and a
  :class:`PrefixCacheOracle`.
* **Facet C — scaling** (sibling): an autoscaler + SLO controller that observes
  :class:`RouterStats` and resizes the :class:`WorkerView` set.

Every cross-facet dependency is one of the ``runtime_checkable`` Protocols below,
so each facet ships and tests independently against deterministic fakes. The
router never imports B or C; it only depends on these shapes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .request import InferenceRequest


@dataclass(frozen=True, slots=True)
class InferenceResult:
    """The outcome of executing one (possibly batched) request on a backend.

    Token counts are *actuals* (so fair-share charging and metrics reflect real
    work, not the worst-case reservation). ``cache_hit`` lets facet B report a
    cache/prefix hit back to the router for stats; ``accepted_tokens`` lets it
    report how many speculatively-drafted tokens the target model accepted.
    """

    request_id: str
    model: str
    output_tokens: int
    prompt_tokens: int = 0
    cache_hit: bool = False
    accepted_tokens: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens


@runtime_checkable
class InferenceBackend(Protocol):
    """A thing that can execute a micro-batch of requests on one worker/engine.

    The router hands a backend a list of co-scheduled requests (a continuous
    batch); the backend returns one :class:`InferenceResult` per request, in any
    order. The backend owns *how* it executes (a single model engine, a remote
    DashScope call funneled through the resilience gateway, a speculative-decode
    wrapper from facet B). It must never raise for a per-request failure — it
    reports that as a result with ``error`` set — but may raise
    :class:`~app.inference.router.errors.BackendError` for a whole-batch fault.
    """

    @property
    def model(self) -> str:
        """The model id this backend serves."""
        ...

    async def execute_batch(self, requests: Sequence[InferenceRequest]) -> list[InferenceResult]:
        """Run a co-scheduled micro-batch and return a result per request."""
        ...


@runtime_checkable
class PrefixCacheOracle(Protocol):
    """Facet-B seam: estimate the warm-prefix overlap for an affinity decision.

    Given a request's ``prefix_key`` and a candidate worker id, returns a
    fraction in ``[0, 1]`` of how much of the request's prompt prefix is already
    resident in that worker's KV cache. The router uses it to bias routing
    toward the warmest worker. The bundled default
    (:class:`~app.inference.router.affinity.ResidencyOracle`) tracks exact
    last-seen prefixes; facet B can swap a smarter (radix-tree) oracle in.
    """

    def warm_fraction(self, prefix_key: str | None, worker_id: str) -> float:
        """Fraction of the prefix resident on ``worker_id`` (``0.0``–``1.0``)."""
        ...


@dataclass(frozen=True, slots=True)
class WorkerView:
    """A read-only snapshot of a worker the scaling facet (C) can act on.

    Exposes only what an autoscaler needs: identity, capacity, current load, and
    health — never the in-flight requests themselves.
    """

    worker_id: str
    model: str
    token_capacity: int
    tokens_in_use: int
    slots_capacity: int
    slots_in_use: int
    healthy: bool

    @property
    def token_utilization(self) -> float:
        if self.token_capacity <= 0:
            return 1.0
        return self.tokens_in_use / self.token_capacity

    @property
    def slot_utilization(self) -> float:
        if self.slots_capacity <= 0:
            return 1.0
        return self.slots_in_use / self.slots_capacity


@runtime_checkable
class WorkerPoolController(Protocol):
    """Facet-C seam: resize the worker set the router schedules over.

    The autoscaler reads :meth:`snapshot` (and the router's own
    :class:`~app.inference.router.metrics.RouterStats`) and calls
    :meth:`add_worker` / :meth:`drain_worker` to react to load + SLO. The router
    implements this so C never reaches into router internals.
    """

    def snapshot(self) -> list[WorkerView]:
        """Current view of every worker."""
        ...

    def add_worker(self, worker_id: str) -> None:
        """Register a freshly-provisioned worker as schedulable."""
        ...

    def drain_worker(self, worker_id: str) -> None:
        """Stop scheduling new work onto ``worker_id`` (graceful scale-down)."""
        ...


@runtime_checkable
class Clock(Protocol):
    """Injectable monotonic clock so the whole router is deterministic in tests."""

    def __call__(self) -> float:
        """Return monotonic seconds."""
        ...


__all__ = [
    "Clock",
    "InferenceBackend",
    "InferenceResult",
    "PrefixCacheOracle",
    "WorkerPoolController",
    "WorkerView",
]
