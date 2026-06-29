"""Aggregate snapshotting — the domain half of the snapshot seam.

An aggregate that wants snapshot-accelerated loads implements :class:`Snapshotter`:
a pure ``snapshot_state() -> dict`` encoder and a ``restore_state(dict)`` decoder.
The :class:`~app.eventsourcing.domain.repository.Repository`, when given a
:class:`~app.eventsourcing.store.snapshots.SnapshotStore`, loads the latest
snapshot, restores it, then replays only the *tail* events.

Encoding is deliberately the aggregate's responsibility (not reflection over its
fields) so a state field's representation can evolve independently of its
in-memory type — the same discipline as event upcasting, applied to snapshots.
A :class:`SnapshotPolicy` decides *when* to write a new snapshot (e.g. every N
events) so the write path stays cheap.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Snapshotter(Protocol):
    """An aggregate that can encode/restore its state for snapshotting.

    ``version`` and the event-replay machinery are handled by the repository; the
    aggregate only encodes its *domain* state (the fields its ``apply`` mutates).
    """

    version: int

    def snapshot_state(self) -> dict[str, object]:
        """Encode the aggregate's current domain state as a JSON-ready mapping."""
        ...

    def restore_state(self, state: Mapping[str, object], *, version: int) -> None:
        """Restore domain state + version from a snapshot (inverse of encode)."""
        ...


@dataclass(frozen=True, slots=True)
class SnapshotPolicy:
    """Decides when the repository should write a fresh snapshot.

    Attributes:
        every_n_events: write a snapshot once a stream's version crosses a new
            multiple of this (``0`` disables snapshot writing). The repository
            compares the version *before* and *after* an append so a single save
            that vaults several multiples still snapshots once.
    """

    every_n_events: int = 50

    def should_snapshot(self, version_before: int, version_after: int) -> bool:
        if self.every_n_events <= 0:
            return False
        # Snapshot when the post-append version reaches/passes the next multiple.
        return (version_after // self.every_n_events) > (version_before // self.every_n_events)


# --------------------------------------------------------------------------- #
# Typed coercion helpers for ``restore_state`` decoders
# --------------------------------------------------------------------------- #


def as_int(value: object, default: int = 0) -> int:
    """Decode a snapshot value as ``int`` (total; falls back to ``default``)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def as_float(value: object, default: float = 0.0) -> float:
    """Decode a snapshot value as ``float`` (total; falls back to ``default``)."""
    if isinstance(value, bool):
        return default
    return float(value) if isinstance(value, (int, float)) else default


def as_str(value: object, default: str = "") -> str:
    """Decode a snapshot value as ``str`` (total; falls back to ``default``)."""
    return value if isinstance(value, str) else default


def as_bool(value: object, default: bool = False) -> bool:
    """Decode a snapshot value as ``bool`` (total; falls back to ``default``)."""
    return value if isinstance(value, bool) else default


__all__ = [
    "SnapshotPolicy",
    "Snapshotter",
    "as_bool",
    "as_float",
    "as_int",
    "as_str",
]
