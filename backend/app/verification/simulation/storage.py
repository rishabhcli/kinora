"""A simulated object store / DB with injectable IO faults (kinora.md §12.3/§12.6
— accepted clips, frames, audio, and the canon vault live in OSS; shot/beat rows
live in the DB).

The render pipeline persists every accepted shot to object storage (clip, last
frame) and to the DB (shot status, defects). Those writes are where a flaky cloud
bites: a transient ``DISK_IO_ERROR`` that must be retried, a ``DISK_WRITE_LOST_ACK``
that *looks* failed but actually landed (so a naive retry would double-write), a
``DISK_STALE_READ`` where the cache served a value behind the source of truth.

This :class:`SimStorage` is a deterministic in-memory key→bytes/JSON store with
those faults gated through :class:`Buggify`. It is intentionally minimal — a
content map plus a fault layer — because the *interesting* behaviour is not the
store, it's how the pipeline's idempotency and retry logic cope when the store
misbehaves. The lost-ack fault in particular is the adversary for the
eventual-consistency invariant: after the storm clears, every accepted shot must
have exactly one durable artifact, never zero and never a double-charge.

Like every seam here it never blocks; "slow disk" is modelled as a latency value
the caller folds into its own scheduling (the runtime advances the virtual clock),
not a real sleep.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.verification.simulation.buggify import Buggify
from app.verification.simulation.faults import FaultKind


class StorageError(RuntimeError):
    """A transient storage IO failure (the caller is expected to retry)."""


@dataclass(slots=True)
class StorageStats:
    """Observable storage behaviour for the report and invariants."""

    reads: int = 0
    writes: int = 0
    read_errors: int = 0
    write_errors: int = 0
    lost_acks: int = 0
    stale_reads: int = 0
    #: Writes that the caller believes failed but which actually persisted
    #: (the lost-ack set) — the eventual-consistency invariant inspects this.
    phantom_keys: set[str] = field(default_factory=set)


class SimStorage:
    """A deterministic key→value store with injectable IO faults.

    Values are opaque (``bytes`` for blobs, any JSON-able object for rows). The
    store is a flat dict; "object store" vs "DB" is just a key-prefix convention
    the caller chooses. Every read/write rolls :class:`Buggify` against the run
    profile before touching the map, so the fault pattern is a pure function of
    the seed.
    """

    __slots__ = ("_buggify", "_data", "_versions", "stats")

    def __init__(self, buggify: Buggify) -> None:
        self._buggify = buggify
        self._data: dict[str, object] = {}
        #: Monotonic version per key — a stale read serves the previous version.
        self._versions: dict[str, list[object]] = {}
        self.stats = StorageStats()

    def put(self, key: str, value: object) -> None:
        """Persist ``value`` under ``key``. May raise (transient) or lose its ack.

        On a ``DISK_WRITE_LOST_ACK`` the data *is* written but the call raises, so
        the caller treats it as failed and (correctly designed) retries against an
        idempotency key rather than blindly re-writing. The written-but-unacked
        key is tracked in ``phantom_keys`` so the eventual-consistency invariant
        can assert the system reconciled it.
        """
        self.stats.writes += 1
        if self._buggify.should(FaultKind.DISK_IO_ERROR, "disk.put", detail=key):
            self.stats.write_errors += 1
            raise StorageError(f"transient write failure for {key!r}")

        # Commit the value (and snapshot for stale-read service).
        self._versions.setdefault(key, []).append(value)
        self._data[key] = value

        if self._buggify.should(FaultKind.DISK_WRITE_LOST_ACK, "disk.put.ack", detail=key):
            self.stats.lost_acks += 1
            self.stats.phantom_keys.add(key)
            raise StorageError(f"write landed but ack was lost for {key!r}")

    def get(self, key: str, default: object = None) -> object:
        """Read ``key``. May raise (transient) or serve a stale prior version."""
        self.stats.reads += 1
        if self._buggify.should(FaultKind.DISK_IO_ERROR, "disk.get", detail=key):
            self.stats.read_errors += 1
            raise StorageError(f"transient read failure for {key!r}")

        versions = self._versions.get(key)
        if (
            versions is not None
            and len(versions) >= 2
            and self._buggify.should(FaultKind.DISK_STALE_READ, "disk.get.stale", detail=key)
        ):
            self.stats.stale_reads += 1
            return versions[-2]
        return self._data.get(key, default)

    def exists(self, key: str) -> bool:
        """Whether ``key`` has a durable value (fault-free — for invariants)."""
        return key in self._data

    def delete(self, key: str) -> None:
        """Remove ``key`` (fault-free)."""
        self._data.pop(key, None)
        self._versions.pop(key, None)
        self.stats.phantom_keys.discard(key)

    def latency_ms(self, op: str) -> int:
        """Roll for slow-disk latency on an op; the caller folds it into timing."""
        return self._buggify.duration(FaultKind.DISK_SLOW, f"disk.slow.{op}")

    @property
    def keys(self) -> list[str]:
        """All durable keys (diagnostics / invariant inspection)."""
        return list(self._data)


__all__ = [
    "SimStorage",
    "StorageError",
    "StorageStats",
]
