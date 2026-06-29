"""Core request types for the inference router.

An :class:`InferenceRequest` is the unit the whole router schedules. It carries
just enough to make every downstream decision *without* the prompt content:

* **identity** — a stable ``request_id`` (idempotency / coalescing) and the
  ``tenant``/``agent`` it is billed to (weighted fair share, §12.2 fairness);
* **priority** — committed work preempts speculative (§12.2 lanes);
* **token budget** — ``prompt_tokens`` + ``max_output_tokens`` drive
  continuous-batch bin-packing (an in-flight batch is bounded by a *token*
  budget, not a request count);
* **a routing key** — the ``prefix_key`` (a hash of the shared system/canon
  prefix) for KV-cache-affinity routing (route same-prefix requests to the same
  worker so its KV cache is reused, §12.3 caching layers);
* **a dedup key** — ``coalesce_key`` so two identical in-flight requests pay for
  one (§12.3 request-level dedup).

Nothing here imports the provider layer: the router is a *scheduling brain* over
an abstract :class:`~app.inference.router.protocols.InferenceBackend`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
from typing import Any

from .errors import RouterConfigError


class RequestPriority(IntEnum):
    """Scheduling priority class. Higher value preempts lower (§12.2 lanes).

    ``COMMITTED`` is confirmed-arrival work the reader is about to watch;
    ``SPECULATIVE`` is prefetch that may be dropped under backpressure;
    ``BULK`` is offline/batch work (Phase-A page analysis, re-scoring) that may
    starve indefinitely under load without harming the live experience.
    """

    BULK = 0
    SPECULATIVE = 1
    COMMITTED = 2
    INTERACTIVE = 3

    @classmethod
    def from_name(cls, name: str) -> RequestPriority:
        """Resolve a case-insensitive name to a priority, raising on unknown."""
        try:
            return cls[name.strip().upper()]
        except KeyError as exc:  # pragma: no cover - defensive
            raise RouterConfigError(f"unknown priority: {name!r}") from exc


class RequestState(StrEnum):
    """Lifecycle of a request inside the router."""

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    COALESCED = "coalesced"
    CANCELLED = "cancelled"


#: States in which a request is no longer occupying scheduling resources.
TERMINAL_STATES: frozenset[RequestState] = frozenset(
    {
        RequestState.SUCCEEDED,
        RequestState.FAILED,
        RequestState.REJECTED,
        RequestState.EXPIRED,
        RequestState.COALESCED,
        RequestState.CANCELLED,
    }
)


def prefix_key_for(text: str, *, max_chars: int | None = None) -> str:
    """Hash a shared-prefix string into a stable, short affinity key.

    ``max_chars`` truncates the *input* before hashing so callers can key on
    just the system/canon prefix (the part actually shared across a tenant's
    requests) rather than the full prompt. Deterministic and content-free in the
    output (only a hex digest is retained), so it is safe to log.
    """
    payload = text if max_chars is None else text[:max_chars]
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]  # noqa: S324 - non-crypto key


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    """One schedulable inference request (content-free at the routing layer).

    Attributes:
        request_id: Stable id; the idempotency unit for coalescing + retries.
        model: Target model id (routing is per-model; pools never mix models).
        tenant: Billing/fairness bucket (a reader/session/org).
        agent: Logical caller (showrunner/adapter/...); a finer fairness key.
        priority: Scheduling class (preemption + weighted fair share).
        prompt_tokens: Estimated prompt length, in tokens (bin-packing input).
        max_output_tokens: Cap on generated tokens (bin-packing + KV reservation).
        prefix_key: Hash of the shared prefix for KV-affinity routing; ``None``
            disables affinity for this request (it can land on any worker).
        coalesce_key: Dedup key; two QUEUED/RUNNING requests with the same
            non-``None`` key share one execution. Defaults to ``request_id``
            (no cross-request coalescing) unless set explicitly.
        enqueued_at: Monotonic seconds the request entered the queue (set by the
            router; ``0.0`` until then).
        queue_sla_s: Max queue wait before the request is dropped as expired;
            ``None`` means no queue-time SLA.
        deadline_s: Optional absolute monotonic deadline (end-to-end); advisory.
        cost_weight: Per-request multiplier on the fair-share cost charged to the
            tenant (e.g. a long generation costs more share than a short one).
        metadata: Opaque caller tags (never inspected by the scheduler).
    """

    request_id: str
    model: str
    tenant: str = "default"
    agent: str = "default"
    priority: RequestPriority = RequestPriority.COMMITTED
    prompt_tokens: int = 0
    max_output_tokens: int = 0
    prefix_key: str | None = None
    coalesce_key: str | None = None
    enqueued_at: float = 0.0
    queue_sla_s: float | None = None
    deadline_s: float | None = None
    cost_weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id:
            raise RouterConfigError("request_id must be non-empty")
        if not self.model:
            raise RouterConfigError("model must be non-empty")
        if self.prompt_tokens < 0 or self.max_output_tokens < 0:
            raise RouterConfigError("token counts must be non-negative")
        if self.cost_weight <= 0:
            raise RouterConfigError("cost_weight must be positive")
        if self.queue_sla_s is not None and self.queue_sla_s <= 0:
            raise RouterConfigError("queue_sla_s must be positive when set")

    @property
    def total_tokens(self) -> int:
        """Worst-case token footprint (prompt + full generation)."""
        return self.prompt_tokens + self.max_output_tokens

    @property
    def effective_coalesce_key(self) -> str:
        """The key used for in-flight dedup (defaults to the request id)."""
        return self.coalesce_key if self.coalesce_key is not None else self.request_id

    def fairness_key(self) -> tuple[str, str]:
        """The (tenant, agent) pair this request is charged against."""
        return (self.tenant, self.agent)

    def share_cost(self, tokens_done: int | None = None) -> float:
        """Fair-share cost to charge: ``cost_weight`` × work done.

        ``tokens_done`` lets the scheduler charge *actual* progress (decode
        tokens emitted) rather than the worst-case reservation, which is what
        makes a fair-share scheduler track real consumption. Falls back to the
        worst-case footprint when actual progress is unknown.
        """
        work = self.total_tokens if tokens_done is None else tokens_done
        return self.cost_weight * float(max(work, 1))

    def with_enqueued_at(self, t: float) -> InferenceRequest:
        """Return a copy stamped with its enqueue time (router-internal)."""
        return replace(self, enqueued_at=t)

    def as_log_fields(self) -> dict[str, Any]:
        """Structured-log-safe fields (ids + counts only — never content)."""
        return {
            "request_id": self.request_id,
            "model": self.model,
            "tenant": self.tenant,
            "agent": self.agent,
            "priority": self.priority.name,
            "prompt_tokens": self.prompt_tokens,
            "max_output_tokens": self.max_output_tokens,
            "prefix_key": self.prefix_key,
        }


__all__ = [
    "TERMINAL_STATES",
    "InferenceRequest",
    "RequestPriority",
    "RequestState",
    "prefix_key_for",
]
