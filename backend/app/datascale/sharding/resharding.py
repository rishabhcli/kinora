"""Online resharding: move/split a key range to a new shard with zero downtime.

Adding capacity means moving some keys to a new shard *while the system keeps
serving them*. A naive "stop, copy, switch" takes an outage; the online protocol
never does. It is a state machine with a strict, rollback-safe order:

    PLANNING → DUAL_WRITE → BACKFILL → VERIFY → CUTOVER → CLEANUP → DONE
                   │            │         │
                   └────────────┴─────────┴──────────► ABORTED (rollback)

1. **DUAL_WRITE.** The router's :class:`MigrationOverlay` starts sending *writes*
   for the moving keys to **both** the source and the target shard. Reads still
   come from the source (authoritative). New writes therefore land on the target
   from this moment on — so the backfill only has to copy what already existed.
2. **BACKFILL.** Copy the existing rows for the moving keys from source → target
   in bounded batches (so we never hold a long transaction or a big lock).
   Because dual-write is already on, a row written during backfill is an upsert,
   not a lost update.
3. **VERIFY.** Compare source and target for the moving keys (row counts +
   checksums). Only a clean verify may proceed; a mismatch goes to ABORTED.
4. **CUTOVER.** Flip the overlay so reads (and the sole write) go to the target.
   This is the only globally-ordered step and it is atomic at the overlay.
5. **CLEANUP.** Stop dual-writing, delete the moved rows from the source, retire
   the overlay entry, and (for a move that empties a shard) drain it.

Every transition is *reversible before CUTOVER*: aborting tears down the overlay
and target rows and leaves the source exactly as it was. After CUTOVER the target
is authoritative and rollback is a forward re-migration, not an undo — so the
state machine refuses to abort past CUTOVER and says so.

The data movement itself is abstracted behind a :class:`ReshardDataMover`
protocol (count / copy-batch / checksum / delete-batch) so the *protocol* is
proven deterministically with an in-memory mover; production supplies a mover
backed by per-shard sessions.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.core.logging import get_logger
from app.datascale.sharding.keys import ShardKey
from app.datascale.sharding.router import MigrationOverlay

logger = get_logger("app.datascale.sharding.resharding")


class ReshardState(enum.Enum):
    """The lifecycle state of one resharding job."""

    PLANNING = "planning"
    DUAL_WRITE = "dual_write"
    BACKFILL = "backfill"
    VERIFY = "verify"
    CUTOVER = "cutover"
    CLEANUP = "cleanup"
    DONE = "done"
    ABORTED = "aborted"

    @property
    def is_terminal(self) -> bool:
        return self in (ReshardState.DONE, ReshardState.ABORTED)

    @property
    def cutover_done(self) -> bool:
        """True once reads have been flipped to the target (rollback is forward-only)."""
        return self in (ReshardState.CUTOVER, ReshardState.CLEANUP, ReshardState.DONE)


# Allowed forward transitions; ABORTED is reachable from any pre-cutover state.
_FORWARD: dict[ReshardState, ReshardState] = {
    ReshardState.PLANNING: ReshardState.DUAL_WRITE,
    ReshardState.DUAL_WRITE: ReshardState.BACKFILL,
    ReshardState.BACKFILL: ReshardState.VERIFY,
    ReshardState.VERIFY: ReshardState.CUTOVER,
    ReshardState.CUTOVER: ReshardState.CLEANUP,
    ReshardState.CLEANUP: ReshardState.DONE,
}


@dataclass(frozen=True, slots=True)
class ReshardPlan:
    """What is moving and where.

    A resharding job moves a *set of keys* (a move/split is expressed as the
    explicit key set it relocates — the directory-overlay primitive). ``source``
    is the shard currently holding them; ``target`` is the new home. ``table`` is
    the logical table family being moved (the mover scopes its copies to it).
    """

    table: str
    keys: tuple[ShardKey, ...]
    source: str
    target: str
    batch_size: int = 500

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("ReshardPlan must move at least one key")
        if self.source == self.target:
            raise ValueError("source and target shards must differ")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")


class ReshardDataMover(Protocol):
    """The data-movement operations a resharding job needs on the shards.

    Production binds these to per-shard sessions (SELECT/INSERT/DELETE scoped to
    the moving keys); tests bind an in-memory implementation. Copy and delete are
    *batched* and must be idempotent (an upsert / a delete-if-present) so a retry
    after a crash is safe.
    """

    async def count(self, shard_id: str, table: str, keys: Sequence[ShardKey]) -> int:
        """Number of rows on ``shard_id`` for ``table`` belonging to ``keys``."""
        ...

    async def copy_batch(
        self,
        source: str,
        target: str,
        table: str,
        keys: Sequence[ShardKey],
        *,
        offset: int,
        limit: int,
    ) -> int:
        """Upsert one batch of rows source→target; return rows copied (0 = done)."""
        ...

    async def checksum(self, shard_id: str, table: str, keys: Sequence[ShardKey]) -> str:
        """A stable digest of the rows for ``keys`` on ``shard_id`` (verify step)."""
        ...

    async def delete_batch(
        self, shard_id: str, table: str, keys: Sequence[ShardKey], *, limit: int
    ) -> int:
        """Delete one batch of rows for ``keys`` from ``shard_id``; return deleted."""
        ...


class ReshardError(RuntimeError):
    """Raised when a resharding job cannot proceed (e.g. verify mismatch)."""


@dataclass(slots=True)
class ReshardProgress:
    """Mutable progress counters surfaced for observability / the admin CLI."""

    state: ReshardState = ReshardState.PLANNING
    rows_backfilled: int = 0
    rows_deleted: int = 0
    source_checksum: str | None = None
    target_checksum: str | None = None
    verified: bool = False
    abort_reason: str | None = None
    history: list[ReshardState] = field(default_factory=lambda: [ReshardState.PLANNING])


#: Callback invoked whenever the overlay changes, so the live router can be
#: rebound atomically (``ShardRouter.with_overlay``). Pure in tests.
OverlayPublisher = Callable[[MigrationOverlay], Awaitable[None]]


@dataclass(slots=True)
class ReshardingJob:
    """Drives one :class:`ReshardPlan` through the online resharding state machine.

    The job owns the :class:`MigrationOverlay` for its moving keys and publishes
    each overlay change through ``publish`` so the live router picks it up. It is
    *crash-recoverable by construction*: each phase is idempotent and the current
    :class:`ReshardState` is the resume point. The phases are exposed
    individually (``begin_dual_write`` … ``cleanup``) and as :meth:`run` (the full
    sequence) so an operator can pause/inspect between phases.
    """

    plan: ReshardPlan
    mover: ReshardDataMover
    publish: OverlayPublisher | None = None
    progress: ReshardProgress = field(default_factory=ReshardProgress)

    @property
    def state(self) -> ReshardState:
        return self.progress.state

    def overlay(self) -> MigrationOverlay:
        """The current migration overlay implied by this job's state.

        DUAL_WRITE..VERIFY: writes go to both homes, reads to the source.
        CUTOVER onward: reads + writes go to the target. Outside that window the
        overlay is empty (the job hasn't started or has finished/aborted).
        """
        if self.state in (ReshardState.DUAL_WRITE, ReshardState.BACKFILL, ReshardState.VERIFY):
            cutover = False
        elif self.state.cutover_done and self.state is not ReshardState.DONE:
            cutover = True
        else:
            return MigrationOverlay()
        moves = dict.fromkeys(self.plan.keys, (self.plan.source, self.plan.target, cutover))
        return MigrationOverlay(moves=moves)

    # -- transitions --------------------------------------------------------- #

    async def run(self) -> ReshardProgress:
        """Run the whole protocol PLANNING → DONE (raises + aborts on failure)."""
        try:
            await self.begin_dual_write()
            await self.backfill()
            await self.verify()
            await self.cutover()
            await self.cleanup()
        except ReshardError:
            # verify-mismatch etc.: abort (only legal pre-cutover) then re-raise.
            if not self.state.cutover_done:
                await self.abort(self.progress.abort_reason or "reshard failed")
            raise
        return self.progress

    async def begin_dual_write(self) -> None:
        self._require(ReshardState.PLANNING)
        await self._transition(ReshardState.DUAL_WRITE)
        logger.info("reshard.dual_write", table=self.plan.table, keys=len(self.plan.keys))

    async def backfill(self) -> None:
        self._require(ReshardState.DUAL_WRITE)
        await self._transition(ReshardState.BACKFILL)
        offset = 0
        while True:
            copied = await self.mover.copy_batch(
                self.plan.source,
                self.plan.target,
                self.plan.table,
                self.plan.keys,
                offset=offset,
                limit=self.plan.batch_size,
            )
            if copied == 0:
                break
            self.progress.rows_backfilled += copied
            offset += copied
        logger.info("reshard.backfilled", rows=self.progress.rows_backfilled)

    async def verify(self) -> None:
        self._require(ReshardState.BACKFILL)
        await self._transition(ReshardState.VERIFY)
        src = await self.mover.checksum(self.plan.source, self.plan.table, self.plan.keys)
        tgt = await self.mover.checksum(self.plan.target, self.plan.table, self.plan.keys)
        self.progress.source_checksum = src
        self.progress.target_checksum = tgt
        if src != tgt:
            self.progress.abort_reason = (
                f"verify mismatch: source={src} target={tgt}"
            )
            logger.error("reshard.verify_mismatch", source=src, target=tgt)
            raise ReshardError(self.progress.abort_reason)
        self.progress.verified = True
        logger.info("reshard.verified", checksum=src)

    async def cutover(self) -> None:
        self._require(ReshardState.VERIFY)
        if not self.progress.verified:
            raise ReshardError("cannot cut over before a successful verify")
        await self._transition(ReshardState.CUTOVER)
        logger.info("reshard.cutover", source=self.plan.source, target=self.plan.target)

    async def cleanup(self) -> None:
        self._require(ReshardState.CUTOVER)
        await self._transition(ReshardState.CLEANUP)
        # Delete the now-migrated rows from the source in bounded batches.
        while True:
            deleted = await self.mover.delete_batch(
                self.plan.source, self.plan.table, self.plan.keys, limit=self.plan.batch_size
            )
            if deleted == 0:
                break
            self.progress.rows_deleted += deleted
        await self._transition(ReshardState.DONE)
        logger.info("reshard.done", deleted=self.progress.rows_deleted)

    async def abort(self, reason: str) -> None:
        """Roll back a pre-cutover job: tear down the target rows + overlay.

        Refuses to abort once cutover has happened — past that point the target
        is authoritative and "undo" would lose the writes that already landed
        there; the correct recovery is a *forward* re-migration the other way.
        """
        if self.state.cutover_done:
            raise ReshardError(
                f"cannot abort after cutover (state={self.state.value}); "
                "roll forward with a reverse reshard instead"
            )
        if self.state.is_terminal:
            return
        # Remove any rows we copied to the target (idempotent delete).
        if self.progress.rows_backfilled or self.state in (
            ReshardState.BACKFILL,
            ReshardState.VERIFY,
        ):
            while True:
                deleted = await self.mover.delete_batch(
                    self.plan.target, self.plan.table, self.plan.keys, limit=self.plan.batch_size
                )
                if deleted == 0:
                    break
        self.progress.abort_reason = reason
        await self._transition(ReshardState.ABORTED)
        logger.warning("reshard.aborted", reason=reason)

    # -- helpers ------------------------------------------------------------- #

    def _require(self, expected: ReshardState) -> None:
        if self.state is not expected:
            raise ReshardError(
                f"illegal transition: in {self.state.value}, expected {expected.value}"
            )

    async def _transition(self, to: ReshardState) -> None:
        # Validate the edge (forward edges + abort).
        if to is ReshardState.ABORTED:
            valid = not self.state.cutover_done
        else:
            valid = _FORWARD.get(self.state) is to
        if not valid:
            raise ReshardError(f"illegal transition {self.state.value} → {to.value}")
        self.progress.state = to
        self.progress.history.append(to)
        if self.publish is not None:
            await self.publish(self.overlay())


# --------------------------------------------------------------------------- #
# In-memory mover (deterministic protocol proof)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class InMemoryReshardMover:
    """A deterministic :class:`ReshardDataMover` over in-memory shard tables.

    ``data: {shard_id: {table: {row_id: (key, payload)}}}``. Copy upserts by
    ``row_id`` (idempotent); delete removes the moving keys' rows. The checksum is
    an order-independent digest of ``(row_id, payload)`` for the moving keys, so a
    correct backfill makes source and target checksums match; tampering with the
    target's rows makes them diverge (the verify-mismatch path tests rely on this).
    """

    data: dict[str, dict[str, dict[str, tuple[ShardKey, str]]]]

    def _table(self, shard_id: str, table: str) -> dict[str, tuple[ShardKey, str]]:
        return self.data.setdefault(shard_id, {}).setdefault(table, {})

    def _rows_for(
        self, shard_id: str, table: str, keys: Sequence[ShardKey]
    ) -> list[tuple[str, tuple[ShardKey, str]]]:
        keyset = set(keys)
        rows = [
            (rid, val)
            for rid, val in self._table(shard_id, table).items()
            if val[0] in keyset
        ]
        return sorted(rows, key=lambda kv: kv[0])

    async def count(self, shard_id: str, table: str, keys: Sequence[ShardKey]) -> int:
        return len(self._rows_for(shard_id, table, keys))

    async def copy_batch(
        self,
        source: str,
        target: str,
        table: str,
        keys: Sequence[ShardKey],
        *,
        offset: int,
        limit: int,
    ) -> int:
        src_rows = self._rows_for(source, table, keys)
        batch = src_rows[offset : offset + limit]
        tgt = self._table(target, table)
        for rid, val in batch:
            tgt[rid] = val
        return len(batch)

    async def checksum(self, shard_id: str, table: str, keys: Sequence[ShardKey]) -> str:
        import hashlib

        rows = self._rows_for(shard_id, table, keys)
        h = hashlib.sha1()
        for rid, (_, payload) in rows:
            h.update(rid.encode())
            h.update(b"\x1f")
            h.update(payload.encode())
            h.update(b"\x1e")
        return h.hexdigest()

    async def delete_batch(
        self, shard_id: str, table: str, keys: Sequence[ShardKey], *, limit: int
    ) -> int:
        rows = self._rows_for(shard_id, table, keys)[:limit]
        tbl = self._table(shard_id, table)
        for rid, _ in rows:
            tbl.pop(rid, None)
        return len(rows)


__all__ = [
    "InMemoryReshardMover",
    "OverlayPublisher",
    "ReshardDataMover",
    "ReshardError",
    "ReshardPlan",
    "ReshardProgress",
    "ReshardState",
    "ReshardingJob",
]
