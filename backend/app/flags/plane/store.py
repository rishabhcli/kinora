"""Persistence seam for the override layer + audit trail (in-memory default).

The plane separates *what the overlays are* (the pure :class:`OverrideLayer`)
from *where they live*. :class:`OverrideStore` is the seam: load the current
layer, save a new one, append an audit record, read recent history. The default
:class:`InMemoryOverrideStore` needs no infrastructure (so the whole plane runs
and is fully tested with zero infra); a future Postgres/Redis-backed store
implements the same protocol without touching the plane.

The in-memory store keeps a bounded audit ring so history stays cheap, and a
snapshot/export round-trips the override layer to a plain dict.
"""

from __future__ import annotations

from collections import deque
from typing import Protocol

from app.flags.plane.audit import PlaneAuditRecord
from app.flags.plane.overrides import OverrideLayer


class OverrideStore(Protocol):
    """The persistence contract the plane writes its override layer through."""

    def load(self) -> OverrideLayer:
        """Return the current override layer (empty if never written)."""
        ...

    def save(self, layer: OverrideLayer, record: PlaneAuditRecord) -> None:
        """Persist ``layer`` and append its ``record`` to the audit trail."""
        ...

    def history(self, *, flag_key: str | None = None, limit: int = 50) -> list[PlaneAuditRecord]:
        """Recent audit records (newest first), optionally filtered by flag key."""
        ...


class InMemoryOverrideStore:
    """A zero-infra :class:`OverrideStore` — holds the layer + a bounded ring."""

    def __init__(self, *, audit_capacity: int = 500) -> None:
        self._layer = OverrideLayer()
        self._audit: deque[PlaneAuditRecord] = deque(maxlen=audit_capacity)

    def load(self) -> OverrideLayer:
        return self._layer

    def save(self, layer: OverrideLayer, record: PlaneAuditRecord) -> None:
        self._layer = layer
        self._audit.append(record)

    def history(
        self, *, flag_key: str | None = None, limit: int = 50
    ) -> list[PlaneAuditRecord]:
        records = list(self._audit)
        if flag_key is not None:
            records = [r for r in records if r.flag_key == flag_key]
        records.reverse()  # newest first
        return records[: max(0, limit)]


__all__ = ["InMemoryOverrideStore", "OverrideStore"]
