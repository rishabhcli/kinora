"""Projection version guard — auto-rebuild when the fold logic changes.

A projection's read model is only valid for the fold logic that produced it. When
a developer changes a projection's handlers in a way that makes already-folded
rows wrong (a renamed field, a new derived column, a corrected calculation), the
materialised view is stale and *replaying alone won't fix it* — the old rows were
written by the old code. The fix is a rebuild.

To make that automatic, a :class:`~app.eventsourcing.projections.projection.Projection`
declares a ``version`` integer; bumping it signals "the fold changed
incompatibly". The version guard compares the projection's current ``version``
against the ``projection_version`` recorded on its checkpoint:

* **equal** → the view is current; proceed with a normal catch-up.
* **different (or no checkpoint yet)** → the view is stale; the projection must be
  **rebuilt** (cleared + replayed from scratch under the new fold) and the
  checkpoint stamped with the new version.

:func:`check_version` returns a :class:`VersionDecision` describing what to do, so
the caller (a runtime, a deploy hook, the registry) can decide whether to rebuild
automatically or merely warn. :class:`VersionGuard` wraps a
:class:`CheckpointStore` to read/stamp the recorded version.

This is intentionally *advisory plumbing*, not magic: it never rebuilds on its
own. A deploy step or the registry calls :meth:`VersionGuard.ensure_current`,
which rebuilds only the projections whose version moved — turning "remember to
rebuild after changing the fold" into a checked invariant.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.eventsourcing.projections.checkpoints import CheckpointStore
from app.eventsourcing.projections.projection import Projection

#: An async callable that performs a rebuild (cleared + replayed from scratch).
RebuildCallback = Callable[[], Awaitable[object]]


class VersionAction(enum.StrEnum):
    """What the version guard recommends for a projection."""

    #: Recorded version matches the code; serve/continue as-is.
    UP_TO_DATE = "up_to_date"
    #: No checkpoint recorded yet; a first build is needed (a no-op rebuild).
    FIRST_BUILD = "first_build"
    #: Recorded version differs from the code; the view is stale → rebuild.
    REBUILD = "rebuild"


@dataclass(frozen=True, slots=True)
class VersionDecision:
    """The version-guard verdict for one projection."""

    projection: str
    code_version: int
    recorded_version: int | None
    action: VersionAction

    @property
    def needs_rebuild(self) -> bool:
        return self.action in (VersionAction.FIRST_BUILD, VersionAction.REBUILD)


def check_version(projection: Projection, recorded_version: int | None) -> VersionDecision:
    """Compare a projection's code ``version`` to the recorded one (pure)."""
    code_version = projection.version
    if recorded_version is None:
        action = VersionAction.FIRST_BUILD
    elif recorded_version == code_version:
        action = VersionAction.UP_TO_DATE
    else:
        action = VersionAction.REBUILD
    return VersionDecision(
        projection=projection.name,
        code_version=code_version,
        recorded_version=recorded_version,
        action=action,
    )


class VersionGuard:
    """Reads/stamps the recorded fold version on a projection's checkpoint."""

    def __init__(self, checkpoints: CheckpointStore) -> None:
        self._checkpoints = checkpoints

    async def decide(self, projection: Projection) -> VersionDecision:
        """The version verdict for ``projection`` against its stored checkpoint.

        A checkpoint that has never been written (no ``updated_at`` and position 0)
        is treated as a FIRST_BUILD — there is no view yet. Otherwise the recorded
        ``projection_version`` is compared against the code's ``version``.
        """
        cp = await self._checkpoints.load(projection.name)
        never_built = cp.updated_at is None and cp.position == 0
        recorded = None if never_built else cp.projection_version
        return check_version(projection, recorded)

    async def stamp(self, projection: Projection) -> None:
        """Record the projection's current code version on its checkpoint."""
        await self._checkpoints.set_projection_version(projection.name, projection.version)

    async def ensure_current(
        self, projection: Projection, rebuild: RebuildCallback
    ) -> VersionDecision:
        """Rebuild ``projection`` iff its fold version moved, then stamp the version.

        ``rebuild`` is an async callable that performs the actual rebuild (e.g.
        ``runtime.rebuild`` or ``registry.runtime(name).rebuild``). It is invoked
        only when the decision :attr:`~VersionDecision.needs_rebuild`. Returns the
        decision so the caller can log/act on what happened.
        """
        decision = await self.decide(projection)
        if decision.needs_rebuild:
            await rebuild()
            await self.stamp(projection)
        return decision


__all__ = [
    "RebuildCallback",
    "VersionAction",
    "VersionDecision",
    "VersionGuard",
    "check_version",
]
