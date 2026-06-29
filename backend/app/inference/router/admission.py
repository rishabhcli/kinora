"""Admission control + backpressure + queue-time SLAs (§12.2).

The router never queues work unbounded. *Before* a request joins the fair-share
queue, the admission controller decides whether to let it in, given:

* **Global queue depth.** A hard ceiling on total queued requests; beyond it the
  queue is shedding.
* **Priority-aware shedding.** Under pressure, *speculative* and *bulk* enqueues
  are dropped first (the §12.2 rule: "new speculative enqueues are dropped, the
  keyframe ladder covers them; committed enqueues are always admitted"). A
  soft watermark below the hard ceiling starts shedding low-priority work while
  still admitting committed/interactive.
* **Per-tenant concurrency.** A cap on a single tenant's *in-flight + queued*
  requests so one reader can't monopolise shared workers (§12.2 per-session
  fairness), with a per-tenant queued-depth cap too.
* **Admissibility.** A request whose worst-case footprint exceeds *every*
  worker's capacity can never be served — reject it immediately with no
  retry hint rather than letting it rot in the queue.

Separately, :class:`QueueTimeSLA` evaluates whether an *already-queued* request
has waited past its SLA and should be dropped at the next tick. Both are pure +
clock-injected, so the simulator exercises backpressure deterministically.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .errors import RouterConfigError
from .request import InferenceRequest, RequestPriority


class RejectReason(StrEnum):
    """Machine-stable admission-rejection reasons (set on :class:`AdmissionRejected`)."""

    QUEUE_FULL = "queue_full"
    TENANT_CONCURRENCY = "tenant_concurrency"
    TENANT_QUEUE_FULL = "tenant_queue_full"
    SHED_LOW_PRIORITY = "shed_low_priority"
    UNSERVABLE = "unservable"


@dataclass(frozen=True, slots=True)
class AdmissionConfig:
    """Tunables for admission control + backpressure.

    Attributes:
        max_queue_depth: Hard cap on total queued requests.
        soft_queue_depth: Depth at which low-priority (``< shed_below``) enqueues
            start being shed; ``None`` → equal to ``max_queue_depth`` (no soft
            zone). Committed/interactive are still admitted up to the hard cap.
        shed_below: Priorities strictly below this are subject to soft-zone
            shedding; defaults to shedding ``SPECULATIVE`` and ``BULK``.
        max_tenant_inflight: Cap on a tenant's queued+running requests; ``None``
            disables the per-tenant concurrency cap.
        max_tenant_queue_depth: Cap on a tenant's *queued* requests; ``None``
            disables it.
        default_retry_after_s: Retry hint attached to backpressure rejections.
    """

    max_queue_depth: int = 1024
    soft_queue_depth: int | None = None
    shed_below: RequestPriority = RequestPriority.COMMITTED
    max_tenant_inflight: int | None = 32
    max_tenant_queue_depth: int | None = None
    default_retry_after_s: float = 0.5

    def __post_init__(self) -> None:
        if self.max_queue_depth <= 0:
            raise RouterConfigError("max_queue_depth must be positive")
        soft = self.soft_queue_depth
        if soft is not None and not 0 < soft <= self.max_queue_depth:
            raise RouterConfigError("soft_queue_depth must be in (0, max_queue_depth]")
        if self.max_tenant_inflight is not None and self.max_tenant_inflight <= 0:
            raise RouterConfigError("max_tenant_inflight must be positive when set")
        if self.max_tenant_queue_depth is not None and self.max_tenant_queue_depth <= 0:
            raise RouterConfigError("max_tenant_queue_depth must be positive when set")
        if self.default_retry_after_s < 0:
            raise RouterConfigError("default_retry_after_s must be non-negative")

    @property
    def effective_soft_depth(self) -> int:
        return self.soft_queue_depth if self.soft_queue_depth is not None else self.max_queue_depth


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """The outcome of an admission check."""

    admit: bool
    reason: RejectReason | None = None
    retry_after_s: float | None = None

    @classmethod
    def accept(cls) -> AdmissionDecision:
        return cls(admit=True)

    @classmethod
    def reject(
        cls, reason: RejectReason, *, retry_after_s: float | None = None
    ) -> AdmissionDecision:
        return cls(admit=False, reason=reason, retry_after_s=retry_after_s)


@dataclass(frozen=True, slots=True)
class LoadSnapshot:
    """The live counts the controller reasons over (supplied by the router)."""

    queue_depth: int
    tenant_inflight: int
    tenant_queue_depth: int
    max_worker_token_capacity: int


class AdmissionController:
    """Decides whether a request may join the queue, and why not if it can't."""

    def __init__(self, config: AdmissionConfig | None = None) -> None:
        self.config = config or AdmissionConfig()

    def evaluate(self, request: InferenceRequest, load: LoadSnapshot) -> AdmissionDecision:
        """Return an admit/reject decision for ``request`` under ``load``."""
        cfg = self.config

        # 1. Unservable: no worker could ever hold it. Never retryable.
        if (
            load.max_worker_token_capacity > 0
            and request.total_tokens > load.max_worker_token_capacity
        ):
            return AdmissionDecision.reject(RejectReason.UNSERVABLE)

        # 2. Hard global ceiling — applies to everyone.
        if load.queue_depth >= cfg.max_queue_depth:
            return AdmissionDecision.reject(
                RejectReason.QUEUE_FULL, retry_after_s=cfg.default_retry_after_s
            )

        # 3. Soft zone: shed low-priority work, keep admitting committed+.
        if load.queue_depth >= cfg.effective_soft_depth and request.priority < cfg.shed_below:
            return AdmissionDecision.reject(
                RejectReason.SHED_LOW_PRIORITY, retry_after_s=cfg.default_retry_after_s
            )

        # 4. Per-tenant concurrency + queued-depth caps.
        if cfg.max_tenant_inflight is not None and load.tenant_inflight >= cfg.max_tenant_inflight:
            return AdmissionDecision.reject(
                RejectReason.TENANT_CONCURRENCY, retry_after_s=cfg.default_retry_after_s
            )
        if (
            cfg.max_tenant_queue_depth is not None
            and load.tenant_queue_depth >= cfg.max_tenant_queue_depth
        ):
            return AdmissionDecision.reject(
                RejectReason.TENANT_QUEUE_FULL, retry_after_s=cfg.default_retry_after_s
            )

        return AdmissionDecision.accept()


class QueueTimeSLA:
    """Evaluates queue-time SLA expiry for queued requests.

    A request carries its own ``queue_sla_s``; an optional ``default_sla_s``
    backstops requests that did not set one. ``clock`` is injectable for
    determinism.
    """

    def __init__(
        self,
        *,
        default_sla_s: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if default_sla_s is not None and default_sla_s <= 0:
            raise RouterConfigError("default_sla_s must be positive when set")
        self._default_sla_s = default_sla_s
        import time

        self._clock = clock or time.monotonic

    def sla_for(self, request: InferenceRequest) -> float | None:
        return request.queue_sla_s if request.queue_sla_s is not None else self._default_sla_s

    def waited(self, request: InferenceRequest, now: float | None = None) -> float:
        t = self._clock() if now is None else now
        return max(0.0, t - request.enqueued_at)

    def is_expired(self, request: InferenceRequest, now: float | None = None) -> bool:
        """Whether ``request`` has waited past its (or the default) SLA."""
        sla = self.sla_for(request)
        if sla is None:
            return False
        return self.waited(request, now) > sla

    def expired_among(
        self, requests: list[InferenceRequest], now: float | None = None
    ) -> list[InferenceRequest]:
        """All requests in ``requests`` that have blown their SLA."""
        t = self._clock() if now is None else now
        return [r for r in requests if self.is_expired(r, t)]


__all__ = [
    "AdmissionConfig",
    "AdmissionController",
    "AdmissionDecision",
    "LoadSnapshot",
    "QueueTimeSLA",
    "RejectReason",
]
