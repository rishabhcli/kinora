"""Digest batching — roll many small notifications into one periodic summary.

A reader who opted into a digest cadence (``DigestCadence.enabled``) does not want
a ping per render; they want a single "here's what happened" message every N
minutes. The digester accumulates digestable notifications per user and, when an
interval has elapsed (or on an explicit flush), folds them into one synthetic
:class:`~app.notifications.events.DomainEvent.DIGEST_READY` notification carrying
a rolled-up summary.

Urgent notifications never enter the digest (they bypass batching entirely — that
policy is enforced upstream by the dispatcher checking ``priority.digestable``).

The accumulator is an injectable seam so a durable (Redis/DB) impl can replace the
in-memory one for cross-process batching; the in-memory one serves tests + a
single-process API.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from app.notifications.events import DomainEvent
from app.notifications.models import Notification


@dataclass(slots=True)
class DigestBucket:
    """A user's accumulating digest window."""

    user_id: str
    opened_at: float
    items: list[Notification] = field(default_factory=list)

    def add(self, notification: Notification) -> None:
        self.items.append(notification)

    @property
    def count(self) -> int:
        return len(self.items)

    def is_due(self, *, now: float, interval_s: float) -> bool:
        """Whether this bucket has been open at least one interval."""
        return self.count > 0 and (now - self.opened_at) >= interval_s

    def summarize(self) -> str:
        """A plain-text rollup grouping items by event kind."""
        by_event: dict[DomainEvent, int] = defaultdict(int)
        for item in self.items:
            by_event[item.event] += 1
        lines = [
            f"  • {count}× {event.value.replace('_', ' ')}"
            for event, count in by_event.items()
        ]
        return "\n".join(lines)


class DigestAccumulator(Protocol):
    """Accumulate + flush per-user digest buckets."""

    async def add(self, notification: Notification, *, now: float) -> None: ...

    async def due(self, *, now: float, interval_s: float) -> list[DigestBucket]: ...

    async def flush(self, user_id: str) -> DigestBucket | None: ...

    async def flush_if_due(
        self, user_id: str, *, now: float, interval_s: float
    ) -> DigestBucket | None: ...

    async def pending_user_ids(self) -> list[str]: ...


class InMemoryDigestAccumulator:
    """A process-local digest accumulator (tests + single-process API)."""

    def __init__(self) -> None:
        self._buckets: dict[str, DigestBucket] = {}

    async def add(self, notification: Notification, *, now: float) -> None:
        bucket = self._buckets.get(notification.recipient.user_id)
        if bucket is None:
            bucket = DigestBucket(user_id=notification.recipient.user_id, opened_at=now)
            self._buckets[notification.recipient.user_id] = bucket
        bucket.add(notification)

    async def due(self, *, now: float, interval_s: float) -> list[DigestBucket]:
        """Pop + return every bucket whose interval has elapsed."""
        ready = [
            user_id
            for user_id, bucket in self._buckets.items()
            if bucket.is_due(now=now, interval_s=interval_s)
        ]
        return [self._buckets.pop(user_id) for user_id in ready]

    async def flush(self, user_id: str) -> DigestBucket | None:
        """Force-pop a user's bucket regardless of its age (``None`` if empty)."""
        return self._buckets.pop(user_id, None)

    async def flush_if_due(
        self, user_id: str, *, now: float, interval_s: float
    ) -> DigestBucket | None:
        """Pop a user's bucket only if its interval has elapsed (else leave it)."""
        bucket = self._buckets.get(user_id)
        if bucket is None or not bucket.is_due(now=now, interval_s=interval_s):
            return None
        return self._buckets.pop(user_id, None)

    async def pending_user_ids(self) -> list[str]:
        """Users with an open, non-empty bucket (observability)."""
        return [uid for uid, b in self._buckets.items() if b.count > 0]


def build_digest_notification(
    bucket: DigestBucket,
    *,
    notification_id: str,
    base: Notification,
) -> Notification:
    """Fold a bucket into one synthetic ``DIGEST_READY`` notification.

    ``base`` supplies the recipient + idempotency scaffolding (any item works);
    the digest's data carries the count + the summary for the template to render.
    """
    return base.model_copy(
        update={
            "id": notification_id,
            "event": DomainEvent.DIGEST_READY,
            "idempotency_key": f"digest:{bucket.user_id}:{int(bucket.opened_at)}",
            "data": {"count": bucket.count, "summary": bucket.summarize()},
            "message": None,
            "book_id": None,
            "session_id": None,
        }
    )


__all__ = [
    "DigestAccumulator",
    "DigestBucket",
    "InMemoryDigestAccumulator",
    "build_digest_notification",
]
