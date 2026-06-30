"""The seams (Protocols) the engine depends on — every one fakeable in a test.

The engine never imports a concrete provider, clock, object store, metrics
backend or logger. It depends only on these structural protocols, so the whole
lifecycle can be driven deterministically with an in-memory store, a fake clock,
a scripted provider and a recording metrics/event sink — no infra, no network,
no real time. Production wiring (DashScope/MiniMax provider adapter, the boto3
:class:`~app.storage.object_store.ObjectStore`, ``structlog``) supplies the same
shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .events import JobEvent
from .models import JobRequest, JobState

# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #


@runtime_checkable
class JobClock(Protocol):
    """Monotonic-ish time + cancellable sleep, both injectable for tests.

    ``now`` returns clock-relative seconds (only differences are meaningful).
    ``sleep`` lets a fake clock advance instantly and deterministically.
    """

    def now(self) -> float:
        """Current time in seconds."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` (a fake clock advances ``now`` without waiting)."""
        ...


# --------------------------------------------------------------------------- #
# Provider adapter
# --------------------------------------------------------------------------- #


class ProviderStatus(StrEnum):
    """Normalized provider task status (each adapter maps its own vocabulary)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: The provider says the task/result no longer exists (TTL elapsed).
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ProviderSubmit:
    """What a provider returns from ``submit``: its async task handle."""

    provider_task_id: str
    #: Provider's own initial status, if it reports one synchronously.
    status: ProviderStatus = ProviderStatus.PENDING


@dataclass(frozen=True, slots=True)
class ProviderPoll:
    """A normalized poll/webhook observation of a provider task."""

    status: ProviderStatus
    #: Present only when ``status == SUCCEEDED``; this URL **expires**.
    clip_url: str | None = None
    #: Provider-supplied failure / expiry message (never a secret).
    error: str | None = None
    #: Server backoff hint (seconds) — when present the poller honours it.
    retry_after_s: float | None = None
    #: Echoed correlation fields for telemetry.
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class VideoJobProvider(Protocol):
    """A hosted async video provider, reduced to the three operations the engine
    needs: submit, poll, and (best-effort) cancel.

    Concrete adapters wrap the existing ``VideoProvider`` (DashScope/Wan) and the
    MiniMax client. Each one owns its status-string → :class:`ProviderStatus`
    mapping and request-shape translation; the engine stays provider-agnostic.
    """

    name: str

    async def submit(self, request: JobRequest, *, idempotency_key: str | None) -> ProviderSubmit:
        """Submit the async render; return the provider task handle.

        Should be idempotent on ``idempotency_key`` when the provider supports it
        (the engine *also* dedups before calling, so this is a second line of
        defence, not a requirement).
        """
        ...

    async def poll(self, provider_task_id: str) -> ProviderPoll:
        """Fetch the current normalized status of an async task."""
        ...

    async def cancel(self, provider_task_id: str) -> None:
        """Best-effort cancel; swallow "already terminal / unsupported" errors."""
        ...

    def parse_webhook(self, payload: dict[str, Any]) -> ProviderPoll | None:
        """Map a provider webhook body to a normalized observation.

        Returns ``None`` if the body is not a recognizable completion for this
        provider (the engine then treats it as unmatched).
        """
        ...

    def webhook_task_id(self, payload: dict[str, Any]) -> str | None:
        """Extract the provider task id a webhook body refers to, if any."""
        ...


# --------------------------------------------------------------------------- #
# Backoff schedule (poll cadence)
# --------------------------------------------------------------------------- #


@runtime_checkable
class PollSchedule(Protocol):
    """Computes the delay before the next poll for a given attempt.

    Mirrors :class:`app.providers.resilience.backoff.BackoffSchedule` but is kept
    as a narrow protocol so the engine never reaches into the resilience package
    and tests can supply a trivial constant schedule.
    """

    def next_delay(self, attempt: int, *, retry_after_s: float | None = None) -> float:
        """Seconds to wait before the ``attempt + 1``-th poll (``attempt`` 1-based)."""
        ...


# --------------------------------------------------------------------------- #
# Asset persistence (object storage)
# --------------------------------------------------------------------------- #


@runtime_checkable
class AssetFetcher(Protocol):
    """Downloads the (expiring) provider clip URL to bytes."""

    async def fetch(self, url: str) -> bytes:
        """Return the clip bytes, or raise on transport failure."""
        ...


@runtime_checkable
class ObjectStorePort(Protocol):
    """The slice of :class:`~app.storage.object_store.ObjectStore` the engine uses.

    Synchronous (boto3 is sync); the engine offloads calls to a thread so the
    event loop is never blocked. A fake in-memory store satisfies this in tests.
    """

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        """Upload ``data`` to ``key``."""
        ...

    def exists(self, key: str) -> bool:
        """Whether an object already lives at ``key`` (idempotent re-persist guard)."""
        ...


# --------------------------------------------------------------------------- #
# Webhook signature verification
# --------------------------------------------------------------------------- #


@runtime_checkable
class WebhookVerifier(Protocol):
    """Verifies a webhook's authenticity before the engine trusts its body.

    Implementations typically HMAC the raw body with a shared secret and compare
    in constant time. Returning ``False`` (or raising) causes the engine to drop
    the webhook without mutating any job.
    """

    def verify(self, *, raw_body: bytes, headers: dict[str, str]) -> bool:
        """Whether the signature on ``raw_body`` is valid."""
        ...


# --------------------------------------------------------------------------- #
# Observability sinks
# --------------------------------------------------------------------------- #


@runtime_checkable
class EventSink(Protocol):
    """Receives every lifecycle :class:`JobEvent`."""

    def emit(self, event: JobEvent) -> None:
        """Handle one lifecycle event (log it, fan it out, record it)."""
        ...


@runtime_checkable
class MetricsSink(Protocol):
    """Counter + histogram surface for engine metrics (Prometheus-shaped)."""

    def incr(self, name: str, *, value: int = 1, **labels: str) -> None:
        """Increment a named counter."""
        ...

    def observe(self, name: str, value: float, **labels: str) -> None:
        """Record a histogram/gauge observation (e.g. a duration in seconds)."""
        ...


@dataclass(frozen=True, slots=True)
class WebhookResult:
    """Outcome of :meth:`VideoJobEngine.handle_webhook`."""

    #: Whether the body was authenticated + matched to a known job.
    accepted: bool
    job_id: str | None = None
    #: The resulting (or unchanged) job state, when matched.
    state: JobState | None = None
    #: ``True`` when the webhook lost a race to the poller (already terminal).
    deduped: bool = False
    reason: str | None = None


__all__ = [
    "AssetFetcher",
    "EventSink",
    "JobClock",
    "MetricsSink",
    "ObjectStorePort",
    "PollSchedule",
    "ProviderPoll",
    "ProviderStatus",
    "ProviderSubmit",
    "VideoJobProvider",
    "WebhookResult",
    "WebhookVerifier",
]
