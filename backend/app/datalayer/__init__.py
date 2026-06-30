"""The consolidated read-model / projection data layer (facet C, productionised).

``app.datalayer`` is a clean, self-contained **read side** built directly on the
*real* event-store contracts in :mod:`app.eventsourcing.store` — the store the
aggregates actually append to (``RecordedEvent`` with a dense, gap-free
``global_position`` and a ``read_all`` / ``last_position`` catch-up surface).

It is intentionally distinct from the earlier, adapter-based read side in
``app.eventsourcing.projections`` (which consumes its own ``StoredEvent``
adapter protocol): this package needs no shim — it reads ``RecordedEvent``
straight from :class:`~app.eventsourcing.store.contracts.EventStore`, decodes the
domain envelope (``{"type","version","data","meta"}`` written by
:func:`app.eventsourcing.domain.events.serialise`), and folds it into queryable
read models.

What it provides
----------------
* a **projection contract** (:mod:`app.datalayer.projector`) — a ``Projection``
  base with a per-event-type handler registry, plus a ``ProjectionRunner`` that
  owns a **checkpointed catch-up subscription** over the global log: idempotent
  apply (per-projection ``event_id`` dedupe), resume-from-checkpoint, and
  **rebuild-from-zero**;
* a **read-model store** seam + deterministic in-memory implementation
  (:mod:`app.datalayer.readmodel`);
* a **checkpoint store** seam + in-memory implementation
  (:mod:`app.datalayer.checkpoints`) using the store's ``global_position``
  semantics (the checkpoint is the highest fully-applied position; resume at
  ``position + 1``);
* a **projection registry** + a ``rebuild_projection(name)`` admin operation
  (:mod:`app.datalayer.registry`);
* a **consistency checker** that verifies a live projection's read model matches
  a fresh rebuild (:mod:`app.datalayer.consistency`);
* three product read models (:mod:`app.datalayer.readmodels`) — a per-book
  render-progress view, a per-session activity view, and a per-shot lifecycle
  board — with thin query repositories.

Everything here is async, pydantic-free at the storage boundary (plain
JSON-able dicts, matching the store's payloads), and dependency-light: the whole
suite runs against :class:`~app.eventsourcing.store.memory.InMemoryEventStore`
and the in-memory read-model / checkpoint stores with **zero infrastructure**.
The Postgres-backed stores + ORM models (:mod:`app.datalayer.models`) and the
one additive migration are for production; nothing in the test path touches them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.datalayer.registry import ProjectionRegistry
    from app.eventsourcing.store.contracts import EventStore

__all__ = [
    "build_default_registry",
]


def build_default_registry(event_store: EventStore, **kwargs: Any) -> ProjectionRegistry:
    """Lazily build a :class:`~app.datalayer.registry.ProjectionRegistry`.

    A thin re-export kept here so importing :mod:`app.datalayer` stays cheap (the
    projection runtime + concrete read models are imported only on call), matching
    the repo's "lazy composition" rule. Forwards to
    :func:`app.datalayer.registry.build_default_registry`.
    """
    from app.datalayer.registry import build_default_registry as _build

    return _build(event_store=event_store, **kwargs)
