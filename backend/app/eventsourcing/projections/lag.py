"""Eventual-consistency lag tracking + read-your-writes (RYW) tokens.

A read model is *eventually* consistent: a command appends an event at some
``global_position``, and a moment later the projection folds it in. Two problems
fall out of that gap, both solved here.

**1. Lag observability.** :class:`LagTracker` snapshots, per projection, how far
behind the store head each checkpoint is — both in positions (``head -
checkpoint``) and (best-effort) in wall-clock seconds. The render-shot status
board and the ops dashboard read this to show "the timeline is N events / ~M ms
behind live". :func:`worst_lag` collapses many projections into the single SLA
number an operator watches.

**2. Read-your-writes.** When a client issues a command, the command side hands
back a :class:`ConsistencyToken` carrying the ``global_position`` its write
landed at. A subsequent read of the projection passes the token; the read API
calls :meth:`LagTracker.has_caught_up` (or awaits :meth:`wait_for`) to ensure
the projection has consumed *at least* that position before answering — so the
client never reads a view that is missing its own just-made write. This is the
standard CQRS "RYW token" pattern, kept transport-agnostic (the token is just an
int + projection name; the API layer serialises it however it likes).

Everything here is pure / in-process and infra-free; it operates over a
:class:`CheckpointStore` and the consumed :class:`EventStore`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from app.eventsourcing.projections.checkpoints import CheckpointStore
from app.eventsourcing.projections.contracts import EventStore, GlobalPosition


@dataclass(frozen=True, slots=True)
class ConsistencyToken:
    """A read-your-writes token: "the projection must have consumed this position".

    Opaque to the client; the read side compares it against the projection's
    checkpoint. ``projection`` is optional — a bare position works as a token for
    *any* projection ("the global log was at least here").
    """

    position: GlobalPosition
    projection: str | None = None
    issued_at: datetime | None = None

    def encode(self) -> str:
        """A compact wire form: ``<position>:<projection>`` (projection may be empty)."""
        return f"{self.position}:{self.projection or ''}"

    @classmethod
    def decode(cls, raw: str) -> ConsistencyToken:
        """Parse :meth:`encode`'s output back into a token."""
        position_str, _, projection = raw.partition(":")
        return cls(position=int(position_str), projection=projection or None)

    @classmethod
    def at_head(cls, position: GlobalPosition, projection: str | None = None) -> ConsistencyToken:
        """Mint a token for a write that landed at ``position``."""
        return cls(position=position, projection=projection, issued_at=datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class LagSnapshot:
    """A point-in-time lag reading for one projection."""

    projection: str
    checkpoint_position: GlobalPosition
    head_position: GlobalPosition
    captured_at: datetime

    @property
    def lag_events(self) -> int:
        """Positions behind head (≥ 0)."""
        return max(0, self.head_position - self.checkpoint_position)

    @property
    def is_caught_up(self) -> bool:
        return self.checkpoint_position >= self.head_position


class LagTracker:
    """Lag readings + read-your-writes gating over a checkpoint + event store."""

    def __init__(self, *, event_store: EventStore, checkpoints: CheckpointStore) -> None:
        self._events = event_store
        self._checkpoints = checkpoints

    async def snapshot(self, projection: str) -> LagSnapshot:
        """A fresh lag reading for one projection (re-reads the live head)."""
        cp = await self._checkpoints.load(projection)
        head = await self._events.head_position()
        return LagSnapshot(
            projection=projection,
            checkpoint_position=cp.position,
            head_position=head,
            captured_at=datetime.now(UTC),
        )

    async def snapshot_all(self, projections: list[str]) -> list[LagSnapshot]:
        """Lag readings for several projections, sharing one head read."""
        head = await self._events.head_position()
        captured = datetime.now(UTC)
        out: list[LagSnapshot] = []
        for name in projections:
            cp = await self._checkpoints.load(name)
            out.append(
                LagSnapshot(
                    projection=name,
                    checkpoint_position=cp.position,
                    head_position=head,
                    captured_at=captured,
                )
            )
        return out

    async def has_caught_up(
        self, token: ConsistencyToken, *, projection: str | None = None
    ) -> bool:
        """Whether the (token's or given) projection has consumed the token's position."""
        name = projection or token.projection
        if name is None:
            raise ValueError("a projection name is required (token carried none)")
        cp = await self._checkpoints.load(name)
        return cp.position >= token.position

    async def wait_for(
        self,
        token: ConsistencyToken,
        *,
        projection: str | None = None,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.02,
    ) -> bool:
        """Block until the projection consumes the token's position (or timeout).

        Returns True if it caught up in time, False on timeout. Used by a read API
        that wants strict read-your-writes: ``if not await tracker.wait_for(tok):
        return stale_response_warning()``.
        """
        name = projection or token.projection
        if name is None:
            raise ValueError("a projection name is required (token carried none)")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while True:
            cp = await self._checkpoints.load(name)
            if cp.position >= token.position:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(poll_interval_s)


def worst_lag(snapshots: list[LagSnapshot]) -> int:
    """The largest ``lag_events`` across snapshots (0 if none) — the SLA number."""
    return max((s.lag_events for s in snapshots), default=0)


__all__ = [
    "ConsistencyToken",
    "LagSnapshot",
    "LagTracker",
    "worst_lag",
]
