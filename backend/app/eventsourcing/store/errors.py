"""Exception hierarchy for the event store.

Every failure that originates inside the store is an :class:`EventStoreError`,
so a consumer can ``except EventStoreError`` to catch the whole family while
still distinguishing the cases that need different handling:

* :class:`OptimisticConcurrencyError` — the expected-version check failed; the
  command handler should reload the aggregate and retry (the §12.1 "duplicate
  Scheduler events can never double-spend" guarantee, generalised).
* :class:`DuplicateEventError` — an ``event_id`` already exists; an idempotent
  re-append (the same logical write replayed) is a no-op, not a crash.
* :class:`StreamNotFoundError` — a read/operation required an existing stream
  that does not exist.
* :class:`SerializationError` — a payload could not be encoded/decoded against
  the registered schema.
* :class:`AppendError` — a generic, non-concurrency append failure.

These are deliberately plain ``Exception`` subclasses (no SQLAlchemy / asyncpg
types leak out) so the in-memory and Postgres stores raise the *same* errors and
the conformance suite can assert on them uniformly.
"""

from __future__ import annotations


class EventStoreError(Exception):
    """Base class for every error raised by the event store."""


class OptimisticConcurrencyError(EventStoreError):
    """The stream's actual version did not match the caller's expectation.

    Carries enough context for a retry loop to log/decide without re-reading:
    the stream id, the version the caller expected, and the version actually
    found at append time.
    """

    def __init__(
        self,
        stream_id: str,
        *,
        expected: object,
        actual: int | None,
    ) -> None:
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"optimistic concurrency conflict on stream {stream_id!r}: "
            f"expected {expected!r}, actual version {actual!r}"
        )


class StreamNotFoundError(EventStoreError):
    """A required stream does not exist."""

    def __init__(self, stream_id: str) -> None:
        self.stream_id = stream_id
        super().__init__(f"stream {stream_id!r} not found")


class DuplicateEventError(EventStoreError):
    """An event with this ``event_id`` already exists in the store.

    Distinct from a concurrency conflict: the *same* logical event was appended
    twice (a retry that already succeeded). Callers that want idempotent appends
    treat this as success.
    """

    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__(f"event {event_id!r} already exists")


class SerializationError(EventStoreError):
    """A payload could not be (de)serialised against its registered schema."""


class AppendError(EventStoreError):
    """A non-concurrency failure while appending (e.g. an empty batch)."""


__all__ = [
    "AppendError",
    "DuplicateEventError",
    "EventStoreError",
    "OptimisticConcurrencyError",
    "SerializationError",
    "StreamNotFoundError",
]
