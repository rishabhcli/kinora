"""Lifecycle events emitted as a :class:`VideoJob` moves through its states.

Events are a thin, structured-log-safe projection of a transition: they carry
ids, the state edge, counters and timing — never the request payload or any
secret. The engine emits one on every meaningful step (submit, running, poll,
each terminal transition, recovery, cancel) to an injected
:class:`~app.video.jobs.ports.EventSink`. A default sink that forwards to
``structlog`` lives in :mod:`app.video.jobs.observability`; tests inject a
recording sink and assert on the sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .models import JobState, VideoJob


class JobEventType(StrEnum):
    """The kinds of lifecycle events the engine emits."""

    SUBMITTED = "video.job.submitted"
    #: A re-submit collapsed onto an existing job via its idempotency key.
    DEDUPED = "video.job.deduped"
    RUNNING = "video.job.running"
    POLLED = "video.job.polled"
    #: Asset download to object storage started / succeeded / failed-attempt.
    ASSET_PERSISTED = "video.job.asset_persisted"
    SUCCEEDED = "video.job.succeeded"
    FAILED = "video.job.failed"
    EXPIRED = "video.job.expired"
    CANCELLED = "video.job.cancelled"
    #: A crashed worker rehydrated this in-flight job from the store.
    RECOVERED = "video.job.recovered"
    #: A webhook and a poll both tried to terminalize; one was a no-op.
    RECONCILED = "video.job.reconciled"
    WEBHOOK_RECEIVED = "video.job.webhook_received"
    #: A webhook arrived that we could not match to a known job.
    WEBHOOK_UNMATCHED = "video.job.webhook_unmatched"


@dataclass(frozen=True, slots=True)
class JobEvent:
    """One lifecycle event (immutable, log-safe)."""

    type: JobEventType
    job_id: str
    provider: str
    at: float
    state: JobState | None = None
    #: ``completed_by`` / reconciliation source where relevant.
    source: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_job(
        cls,
        type_: JobEventType,
        job: VideoJob,
        *,
        at: float,
        source: str | None = None,
        **detail: Any,
    ) -> JobEvent:
        """Project a :class:`VideoJob` snapshot into an event."""
        return cls(
            type=type_,
            job_id=job.id,
            provider=job.provider,
            at=at,
            state=job.state,
            source=source or job.completed_by,
            detail=detail,
        )

    def as_log_fields(self) -> dict[str, Any]:
        """Flatten to structured-log fields."""
        fields: dict[str, Any] = {
            "event": self.type.value,
            "job_id": self.job_id,
            "provider": self.provider,
            "at": round(self.at, 3),
        }
        if self.state is not None:
            fields["state"] = self.state.value
        if self.source is not None:
            fields["source"] = self.source
        fields.update(self.detail)
        return fields


__all__ = ["JobEvent", "JobEventType"]
