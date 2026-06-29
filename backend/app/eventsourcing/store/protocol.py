"""The :class:`EventStore` protocol consumed by the write side (facet A seam).

This is the **minimal** contract facet B (the command + aggregate model) needs
from facet A (persistence). It is deliberately small and storage-agnostic:

* a stream is identified by an opaque ``stream_id`` string;
* events are appended *optimistically* against an :class:`ExpectedVersion`
  (the version the writer believed the stream was at), and a concurrent writer
  bumps that version so the loser gets a :class:`ConcurrencyError`;
* events are loaded back as ordered :class:`StoredEvent` records, each carrying
  the serialised domain-event envelope plus its assigned stream version.

The domain layer never serialises or deserialises here — it hands the store a
``payload`` mapping (already the envelope dict produced by
:func:`app.eventsourcing.domain.events.serialise`) and gets the same shape back.
Facet A owns the JSON/SQL/snapshot mechanics behind this protocol.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: The version of a stream that has never been written. ``expected_version``
#: equal to this on the first append asserts "this stream does not exist yet".
EVENT_STORE_BEGINNING: int = 0

#: An ``expected_version`` for an append. An ``int`` asserts the stream is at
#: exactly that version (0 == "must not exist yet"); ``None`` means "append
#: regardless of the current version" (no optimistic check).
ExpectedVersion = int | None


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One persisted event as the store hands it back on load.

    Attributes:
        stream_id: the stream this event belongs to.
        version: the 1-based position of this event within its stream. The
            first event in a stream is version 1; the stream's *current version*
            is the version of its last event (0 when empty).
        event_type: the registered domain-event type name (e.g.
            ``"SessionStarted"``) — the discriminator the upcaster/registry keys on.
        event_version: the schema version of ``payload`` *as stored*. Upcasters
            migrate this up to the current schema on load.
        payload: the serialised event body (the envelope's ``data`` block).
        metadata: causation/correlation/actor metadata (the envelope's ``meta``).
        global_position: an optional store-wide monotonic position for building
            read models across streams. ``None`` when the store does not expose one.
    """

    stream_id: str
    version: int
    event_type: str
    event_version: int
    payload: Mapping[str, object]
    metadata: Mapping[str, object]
    global_position: int | None = None


@dataclass(frozen=True, slots=True)
class AppendResult:
    """The outcome of a successful append.

    Attributes:
        stream_id: the stream written to.
        first_version: the version assigned to the first appended event.
        last_version: the new *current version* of the stream (== the version of
            the last appended event); aggregates fast-forward their version to this.
    """

    stream_id: str
    first_version: int
    last_version: int


class ConcurrencyError(RuntimeError):
    """Raised when an append's ``expected_version`` does not match the store.

    The command bus catches this and retries the decision against the freshly
    re-loaded stream (optimistic-concurrency retry, see
    :mod:`app.eventsourcing.domain.concurrency`).
    """

    def __init__(self, stream_id: str, expected: ExpectedVersion, actual: int) -> None:
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"optimistic-concurrency conflict on stream {stream_id!r}: "
            f"expected version {expected!r}, store is at {actual}"
        )


class StreamNotFoundError(KeyError):
    """Raised by stores that distinguish 'absent' from 'empty' on a strict load."""

    def __init__(self, stream_id: str) -> None:
        self.stream_id = stream_id
        super().__init__(stream_id)


@runtime_checkable
class EventStore(Protocol):
    """The append-only event store, optimistic on a per-stream version.

    Implementations live in facet A. The write side only ever depends on this
    protocol so it can be tested against the in-memory fake here.
    """

    async def append(
        self,
        stream_id: str,
        events: Sequence[Mapping[str, object]],
        *,
        expected_version: ExpectedVersion,
    ) -> AppendResult:
        """Append ``events`` to ``stream_id`` atomically and optimistically.

        Each element of ``events`` is a serialised envelope ``{"type", "version",
        "data", "meta"}`` (see :func:`app.eventsourcing.domain.events.serialise`).

        Args:
            stream_id: the target stream.
            events: the ordered envelopes to append (must be non-empty in practice).
            expected_version: the version the writer believes the stream is at.
                An ``int`` is checked against the store's current version; on a
                mismatch a :class:`ConcurrencyError` is raised and *nothing* is
                written. ``None`` skips the check.

        Returns:
            An :class:`AppendResult` with the assigned version range.

        Raises:
            ConcurrencyError: ``expected_version`` did not match the store.
        """
        ...

    async def load(
        self,
        stream_id: str,
        *,
        from_version: int = 0,
    ) -> Sequence[StoredEvent]:
        """Load a stream's events in order, from ``from_version`` (exclusive).

        Args:
            stream_id: the stream to read.
            from_version: return only events with ``version > from_version``
                (0 == from the beginning). Used to replay on top of a snapshot.

        Returns:
            The ordered events; an empty sequence for an unknown/empty stream.
        """
        ...

    async def current_version(self, stream_id: str) -> int:
        """Return the stream's current version (0 when empty/unknown)."""
        ...


__all__ = [
    "EVENT_STORE_BEGINNING",
    "AppendResult",
    "ConcurrencyError",
    "EventStore",
    "ExpectedVersion",
    "StoredEvent",
    "StreamNotFoundError",
]
