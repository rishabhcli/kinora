"""Blue-green projection rebuilds — replay into a spare slot, then swap.

Rebuilding a projection in place (:meth:`ProjectionRuntime.rebuild`) clears the
live read model and replays from scratch — fine offline, but it makes the view
*unavailable* for the duration. A blue-green rebuild keeps the live view serving
reads while a fresh copy is built into a parallel slot, then atomically flips
which slot is "active".

**Slots.** Each projection owns two namespaces, suffixed ``::blue`` and
``::green`` (see :func:`slot_namespace`). A tiny :class:`SlotDirectory` records
which slot is *active* (serves reads) per projection; the other slot is the
*standby* a rebuild targets. :class:`InMemorySlotDirectory` is the test/embedded
implementation; production stores the pointer in Postgres
(:mod:`app.eventsourcing.projections.bluegreen_pg`).

**The dance** (:meth:`BlueGreenRebuilder.rebuild`):

1. Read the active slot; pick the standby (the other colour).
2. Clear the standby namespace and reset a *standby-scoped* checkpoint.
3. Catch the standby up to head with a normal :class:`ProjectionRuntime`
   pointed at the standby namespace + checkpoint name.
4. Atomically flip the directory pointer to the standby.
5. Leave the now-inactive old slot intact (cheap instant rollback) — callers may
   ``clear`` it later.

Reads always go through :meth:`BlueGreenRebuilder.active_namespace`, so a reader
never needs to know which colour is live. The checkpoint for each slot is keyed
``<projection>::<colour>`` so blue and green track independent positions; the
canonical projection name stays the stable identity.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.eventsourcing.projections.checkpoints import CheckpointStore
from app.eventsourcing.projections.contracts import EventStore, StoredEvent
from app.eventsourcing.projections.projection import Projection
from app.eventsourcing.projections.readmodel import ReadModelStore
from app.eventsourcing.projections.runtime import (
    CatchUpResult,
    ProjectionRuntime,
    RuntimeConfig,
)


class Slot(enum.StrEnum):
    """The two parallel read-model slots a projection alternates between."""

    BLUE = "blue"
    GREEN = "green"

    @property
    def other(self) -> Slot:
        return Slot.GREEN if self is Slot.BLUE else Slot.BLUE


def slot_namespace(projection_name: str, slot: Slot) -> str:
    """The read-model namespace for ``projection_name`` in ``slot``."""
    return f"{projection_name}::{slot.value}"


def slot_checkpoint_name(projection_name: str, slot: Slot) -> str:
    """The checkpoint identity for ``projection_name`` in ``slot`` (independent positions)."""
    return f"{projection_name}::{slot.value}"


@runtime_checkable
class SlotDirectory(Protocol):
    """Records which :class:`Slot` is active (serves reads) per projection."""

    async def active(self, projection: str) -> Slot:
        """The slot currently serving reads (default BLUE for a never-built projection)."""
        ...

    async def set_active(self, projection: str, slot: Slot) -> None:
        """Atomically flip the active slot for ``projection``."""
        ...

    async def has_active(self, projection: str) -> bool:
        """Whether a blue/green slot has ever been assigned for ``projection``.

        When False, the projection was never blue/green-rebuilt and its read model
        lives in the *bare* namespace (the runtime's default), not a coloured slot.
        """
        ...


class InMemorySlotDirectory:
    """A deterministic in-process :class:`SlotDirectory`."""

    def __init__(self) -> None:
        self._active: dict[str, Slot] = {}

    async def has_active(self, projection: str) -> bool:
        return projection in self._active

    async def active(self, projection: str) -> Slot:
        return self._active.get(projection, Slot.BLUE)

    async def set_active(self, projection: str, slot: Slot) -> None:
        self._active[projection] = slot

    def snapshot(self) -> dict[str, Slot]:
        return dict(self._active)


@dataclass(slots=True)
class RebuildReport:
    """The outcome of a blue-green rebuild."""

    projection: str
    from_slot: Slot
    to_slot: Slot
    catch_up: CatchUpResult
    swapped: bool


class BlueGreenRebuilder:
    """Orchestrates zero-downtime rebuilds and active-slot lookups for a projection set."""

    def __init__(
        self,
        *,
        event_store: EventStore,
        read_models: ReadModelStore,
        checkpoints: CheckpointStore,
        directory: SlotDirectory,
        config: RuntimeConfig | None = None,
    ) -> None:
        self._events = event_store
        self._read_models = read_models
        self._checkpoints = checkpoints
        self._directory = directory
        self._config = config or RuntimeConfig()

    async def active_namespace(self, projection_name: str) -> str:
        """The read-model namespace a reader should query for ``projection_name``.

        A projection that has never been blue/green-rebuilt has no slot assigned;
        its read model lives in the *bare* namespace (the runtime's default), so
        we return that. Once rebuilt, reads resolve to the active coloured slot.
        """
        if not await self._directory.has_active(projection_name):
            return projection_name
        slot = await self._directory.active(projection_name)
        return slot_namespace(projection_name, slot)

    def runtime_for_active(self, projection: Projection, namespace: str) -> ProjectionRuntime:
        """A runtime bound to a specific (already-resolved) active namespace.

        Used by the live-tail supervisor to keep the *active* slot current after a
        swap: it must continue folding new events into whichever colour is live.
        """
        return ProjectionRuntime(
            projection,
            event_store=self._events,
            read_models=self._read_models,
            checkpoints=self._checkpoints,
            namespace=namespace,
            config=self._config,
        )

    async def rebuild(
        self, projection: Projection, *, clear_old: bool = False
    ) -> RebuildReport:
        """Build ``projection`` into the standby slot, then swap it live.

        If ``clear_old`` is set, the previously-active slot is cleared after the
        swap (reclaims space; forfeits instant rollback). Default keeps it.
        """
        name = projection.name
        from_slot = await self._directory.active(name)
        to_slot = from_slot.other
        standby_ns = slot_namespace(name, to_slot)
        standby_cp = slot_checkpoint_name(name, to_slot)

        # Prepare the standby: empty namespace + a reset, slot-scoped checkpoint.
        await projection.on_reset(self._read_models, standby_ns)
        await self._read_models.clear(standby_ns)
        await self._checkpoints.reset(standby_cp)

        # The runtime keys its checkpoint off ``projection.name``; for a blue/green
        # rebuild blue and green must track *independent* positions, so the standby
        # build runs against a renamed proxy projection (same fold, slot-scoped
        # checkpoint name) rather than leaking slot awareness into the runtime.
        scoped = _RenamedProjection(projection, standby_cp)
        runtime = ProjectionRuntime(
            scoped,
            event_store=self._events,
            read_models=self._read_models,
            checkpoints=self._checkpoints,
            namespace=standby_ns,
            config=self._config,
        )
        catch_up = await runtime.catch_up()

        # Flip the pointer — the swap is the single atomic step.
        await self._directory.set_active(name, to_slot)

        if clear_old:
            await self._read_models.clear(slot_namespace(name, from_slot))

        return RebuildReport(
            projection=name,
            from_slot=from_slot,
            to_slot=to_slot,
            catch_up=catch_up,
            swapped=True,
        )


class _RenamedProjection(Projection):
    """Forwards to a wrapped projection but reports a different ``name``.

    Used so blue/green slots checkpoint independently without the runtime needing
    slot awareness. ``apply`` delegates entirely; the registry/handler machinery
    of the wrapped class is bypassed in favour of its concrete ``apply``.
    """

    def __init__(self, inner: Projection, name: str) -> None:
        self.name = name
        self.version = inner.version
        self._inner = inner

    def interested_in(self) -> frozenset[str] | None:
        return self._inner.interested_in()

    async def apply(
        self, store: ReadModelStore, namespace: str, event: StoredEvent
    ) -> None:
        await self._inner.apply(store, namespace, event)

    async def on_reset(self, store: ReadModelStore, namespace: str) -> None:
        await self._inner.on_reset(store, namespace)


__all__ = [
    "BlueGreenRebuilder",
    "InMemorySlotDirectory",
    "RebuildReport",
    "Slot",
    "SlotDirectory",
    "slot_checkpoint_name",
    "slot_namespace",
]
