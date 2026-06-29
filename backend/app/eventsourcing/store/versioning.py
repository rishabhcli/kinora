"""Stream version semantics and the optimistic-concurrency algebra.

A stream's **version** is a dense, 0-based counter: the first event in a stream
has version ``0``, the next ``1``, and so on. The *current version* of a stream
is the version of its last event, or :data:`NO_EVENTS` (``-1``) when the stream
is empty / absent. This convention makes "append after version N" read naturally
and makes :data:`NO_STREAM` equivalent to "current version is ``-1``".

:class:`ExpectedVersion` is the optimistic-concurrency expectation a writer
passes to :meth:`~app.eventsourcing.store.contracts.EventStore.append`:

==================  =========================================================
Expectation         Append succeeds iff …
==================  =========================================================
``ANY``             always (no concurrency guard)
``NO_STREAM``       the stream is empty/absent (current version == -1)
``STREAM_EXISTS``   the stream has at least one event (current version >= 0)
exact ``int`` N     the current version equals N
==================  =========================================================

The :func:`check` function is the single, pure decision used by *both* the
in-memory and Postgres stores, so their concurrency semantics cannot drift.
"""

from __future__ import annotations

import enum
from typing import TypeAlias

#: The "version" of an empty / non-existent stream. The first appended event
#: takes version 0, so a brand-new stream is at -1 before its first append.
NO_EVENTS: int = -1


class StreamState(enum.Enum):
    """The non-numeric expected-version sentinels."""

    #: Append regardless of current version (no optimistic guard).
    ANY = "any"
    #: Require the stream to be empty / not yet created.
    NO_STREAM = "no_stream"
    #: Require the stream to already have at least one event.
    STREAM_EXISTS = "stream_exists"


#: An expected version is either a state sentinel or an exact (>= 0) version int.
#: A ``TypeAlias`` (PEP 613) so it is a runtime value usable in ``isinstance``-free
#: annotations even with ``from __future__ import annotations`` active.
ExpectedVersion: TypeAlias = "StreamState | int"

# Convenient re-exports so callers can write ``ExpectedVersion.ANY``-style code
# via the module without importing StreamState explicitly.
ANY = StreamState.ANY
NO_STREAM = StreamState.NO_STREAM
STREAM_EXISTS = StreamState.STREAM_EXISTS


def describe(expected: ExpectedVersion) -> str:
    """Human-readable form of an expectation (for error messages / logs)."""
    if isinstance(expected, StreamState):
        return expected.value
    return f"version=={expected}"


def normalize(expected: ExpectedVersion) -> ExpectedVersion:
    """Validate and canonicalise an expectation.

    Raises :class:`ValueError` for a negative exact version other than the
    reserved :data:`NO_EVENTS` (which callers should express as ``NO_STREAM``).
    """
    if isinstance(expected, StreamState):
        return expected
    if isinstance(expected, bool):  # bool is an int subclass — reject explicitly
        raise ValueError("expected_version must be an int or StreamState, not bool")
    if not isinstance(expected, int):
        raise ValueError(f"expected_version must be int or StreamState, got {type(expected)!r}")
    if expected == NO_EVENTS:
        return NO_STREAM
    if expected < 0:
        raise ValueError(f"exact expected_version must be >= 0, got {expected}")
    return expected


def is_satisfied(expected: ExpectedVersion, current_version: int) -> bool:
    """Return whether ``current_version`` satisfies the ``expected`` guard.

    ``current_version`` is :data:`NO_EVENTS` for an empty/absent stream.
    """
    expected = normalize(expected)
    if expected is StreamState.ANY:
        return True
    if expected is StreamState.NO_STREAM:
        return current_version == NO_EVENTS
    if expected is StreamState.STREAM_EXISTS:
        return current_version >= 0
    # exact version
    return current_version == expected


def check(stream_id: str, expected: ExpectedVersion, current_version: int) -> None:
    """Raise :class:`OptimisticConcurrencyError` if the guard is not satisfied.

    The single concurrency decision shared by every store implementation.
    """
    if not is_satisfied(expected, current_version):
        from app.eventsourcing.store.errors import OptimisticConcurrencyError

        raise OptimisticConcurrencyError(
            stream_id,
            expected=describe(expected),
            actual=None if current_version == NO_EVENTS else current_version,
        )


__all__ = [
    "ANY",
    "NO_EVENTS",
    "NO_STREAM",
    "STREAM_EXISTS",
    "ExpectedVersion",
    "StreamState",
    "check",
    "describe",
    "is_satisfied",
    "normalize",
]
