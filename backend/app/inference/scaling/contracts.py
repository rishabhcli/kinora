"""The protocols facet C consumes from the router facet (facet A) — kinora.md §12.

The autoscaling + SLO brain in this package is a *consumer* of two seams that the
router facet owns:

* **InferenceBackend** — one routable target: a model behind a pool of workers on
  some instance type. The scaler asks it for its identity, capacity, and the
  cost/latency knobs it exposes; it never *calls* the model (no generation here).
* **RouterMetricsSource** — the live telemetry the router publishes: per-backend
  in-flight counts, queue depth, a latency digest, and a health flag. The
  autoscaler reads these to decide scale; the SLO router reads them to pick a
  target.

We express both as :func:`typing.runtime_checkable` :class:`~typing.Protocol`\\ s
(PEP 544) so facet A's concrete classes satisfy them *structurally* — no import
dependency, no shared base class, no coupling of release order. The scaling facet
ships its own tiny conforming fakes (:class:`FakeBackend`, :class:`FakeMetrics`)
so every model here is unit-testable before facet A lands on disk.

Latency is reported through the reliability toolkit's :class:`LatencySummary`
(already the project's percentile vocabulary), so SLO evaluation reuses the
existing :mod:`app.reliability.slo` machinery verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.reliability.latency import LatencySummary

__all__ = [
    "BackendId",
    "BackendKind",
    "BackendDescriptor",
    "BackendHealth",
    "BackendTelemetry",
    "InferenceBackend",
    "RouterMetricsSource",
    "FakeBackend",
    "FakeMetrics",
]

#: A backend is addressed by an opaque string id (e.g. ``"wan-i2v-turbo@gpu-l20"``).
BackendId = str


class BackendKind(StrEnum):
    """The model class a backend serves (drives default cost/latency priors)."""

    #: Wan text/image→video — the scarce, slow, expensive lane (§4.4 committed).
    VIDEO = "video"
    #: Image-gen keyframes — the cheap speculative lane (§4.4).
    IMAGE = "image"
    #: Text-to-speech narration (§9).
    TTS = "tts"
    #: LLM reasoning / planning calls (the agent crew, §7).
    REASONING = "reasoning"


class BackendHealth(StrEnum):
    """A backend's coarse health, as published by the router's probes."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"  # serving but slow / partial — deprioritise, don't evict
    UNHEALTHY = "unhealthy"  # not serving — route away, do not enqueue


@dataclass(frozen=True, slots=True)
class BackendDescriptor:
    """The static identity + capacity of one routable backend (facet A → facet C).

    ``concurrency`` is the number of in-flight requests one *worker* of this
    backend serves simultaneously; ``service_time_s`` is the mean per-request
    wall-clock at that concurrency (the §4.1 30–90s for video). ``instance_type``
    names the heterogeneous hardware (drives the cost model, §scaling.instances).
    """

    backend_id: BackendId
    kind: BackendKind
    instance_type: str
    concurrency: int = 1
    service_time_s: float = 5.0
    #: Free-form weights the router/optimiser can read (quality, region, etc.).
    tags: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if self.service_time_s <= 0.0:
            raise ValueError("service_time_s must be positive")

    @property
    def throughput_per_worker_per_s(self) -> float:
        """Requests/second a single warm worker of this backend clears."""
        return self.concurrency / self.service_time_s


@dataclass(frozen=True, slots=True)
class BackendTelemetry:
    """A point-in-time snapshot of one backend's load + health (facet A → facet C).

    ``warm_workers`` is the count of workers currently *able to serve* (past
    cold-start); ``inflight`` is requests currently executing; ``queue_depth`` is
    requests waiting for a free slot. ``latency`` is the rolling percentile digest
    the router maintains, in the reliability toolkit's vocabulary so SLO
    evaluation reuses :mod:`app.reliability.slo`.
    """

    backend_id: BackendId
    warm_workers: int
    inflight: int
    queue_depth: int
    latency: LatencySummary
    health: BackendHealth = BackendHealth.HEALTHY

    @property
    def utilisation(self) -> float:
        """Fraction of warm capacity in use right now (``inflight / slots``).

        Slots are ``warm_workers × concurrency`` upstream; here we approximate one
        slot per warm worker when concurrency is unknown to the telemetry source —
        callers that need exact slots pass it via the descriptor.
        """
        slots = max(1, self.warm_workers)
        return self.inflight / slots

    @property
    def is_routable(self) -> bool:
        """True when the router may send new work here (healthy or degraded)."""
        return self.health is not BackendHealth.UNHEALTHY


@runtime_checkable
class InferenceBackend(Protocol):
    """A routable model target (facet A). The scaler reads it; it never calls it."""

    def descriptor(self) -> BackendDescriptor:
        """The static identity + capacity of this backend."""
        ...


@runtime_checkable
class RouterMetricsSource(Protocol):
    """The live telemetry the router publishes per backend (facet A → facet C)."""

    def backend_ids(self) -> tuple[BackendId, ...]:
        """All backends the router currently knows about."""
        ...

    def telemetry(self, backend_id: BackendId) -> BackendTelemetry:
        """The current load + health snapshot for one backend."""
        ...


# --------------------------------------------------------------------------- #
# Conforming fakes — facet C's own test doubles (no facet-A dependency)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FakeBackend:
    """A trivial :class:`InferenceBackend` for tests + the simulator harness."""

    _descriptor: BackendDescriptor

    def descriptor(self) -> BackendDescriptor:
        return self._descriptor


@dataclass
class FakeMetrics:
    """A mutable :class:`RouterMetricsSource` the simulator/tests drive directly."""

    snapshots: dict[BackendId, BackendTelemetry] = field(default_factory=dict)

    def set(self, telemetry: BackendTelemetry) -> None:
        """Install/replace one backend's snapshot."""
        self.snapshots[telemetry.backend_id] = telemetry

    def backend_ids(self) -> tuple[BackendId, ...]:
        return tuple(self.snapshots.keys())

    def telemetry(self, backend_id: BackendId) -> BackendTelemetry:
        return self.snapshots[backend_id]
