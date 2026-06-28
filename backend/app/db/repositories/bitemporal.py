"""Repositories for the bitemporal canon engine (kinora.md §8).

Three repos over the three additive tables. They own persistence + the temporal read
queries; the *policy* (interval algebra, CRDT stamping, hash-chaining) lives in the
:mod:`app.memory` services. Like every repo here they **flush, never commit** — the
unit-of-work boundary owns the transaction.

* :class:`BitemporalStateRepo` — assert / correct / retire / as-of reads over
  ``bitemporal_states`` (4-D: book × branch × valid-beat × tx-time).
* :class:`CanonAuditRepo` — append + replay the hash-chained audit log.
* :class:`CanonBranchRepo` — the branch registry.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import ColumnElement, and_, func, or_, select, text, update

from app.db.base import new_id
from app.db.models.bitemporal import (
    AuditAction,
    BitemporalState,
    BranchStatus,
    CanonAudit,
    CanonBranch,
)
from app.db.repositories.base import BaseRepository


def _branch_lock_key(book_id: str, branch: str) -> int:
    """A stable 63-bit signed advisory-lock key for one (book, branch) write line.

    Serializes concurrent writers on the same branch so tx-interval closes and the audit
    ``seq`` allocation are race-free (released on transaction end).
    """
    raw = f"kinora:bitemporal:{book_id}:{branch}".encode()
    return int.from_bytes(hashlib.sha1(raw).digest()[:8], "big", signed=True)


class BitemporalStateRepo(BaseRepository):
    """Bitemporal continuity facts: assert, correct (close tx), retire (close valid), read."""

    async def advisory_lock(self, book_id: str, branch: str) -> None:
        """Take the (book, branch) write lock for the surrounding transaction."""
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:k)").bindparams(
                k=_branch_lock_key(book_id, branch)
            )
        )

    async def insert(
        self,
        *,
        book_id: str,
        fact_key: str,
        branch: str,
        subject_entity_key: str,
        predicate: str,
        object_value: str,
        valid_from_beat: int,
        valid_to_beat: int | None,
        tx_from: datetime,
        stamp_wall: int,
        stamp_counter: int,
        actor_id: str,
        source_span: dict | None = None,
        state_id: str | None = None,
    ) -> BitemporalState:
        """Insert one belief row (the physical unit; a logical fact spans many of these)."""
        row = BitemporalState(
            id=state_id or new_id(),
            book_id=book_id,
            fact_key=fact_key,
            branch=branch,
            subject_entity_key=subject_entity_key,
            predicate=predicate,
            object_value=object_value,
            valid_from_beat=valid_from_beat,
            valid_to_beat=valid_to_beat,
            tx_from=tx_from,
            tx_to=None,
            stamp_wall=stamp_wall,
            stamp_counter=stamp_counter,
            actor_id=actor_id,
            source_span=source_span,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, state_id: str) -> BitemporalState | None:
        return await self.session.get(BitemporalState, state_id)

    async def current_belief(
        self, book_id: str, fact_key: str, branch: str
    ) -> BitemporalState | None:
        """The row currently believed for a logical fact on a branch (``tx_to IS NULL``)."""
        stmt = (
            select(BitemporalState)
            .where(
                BitemporalState.book_id == book_id,
                BitemporalState.fact_key == fact_key,
                BitemporalState.branch == branch,
                BitemporalState.tx_to.is_(None),
            )
            .order_by(BitemporalState.tx_from.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def close_tx(self, state_id: str, tx_to: datetime) -> None:
        """Close a belief's transaction interval (a correction supersedes it)."""
        await self.session.execute(
            update(BitemporalState)
            .where(BitemporalState.id == state_id, BitemporalState.tx_to.is_(None))
            .values(tx_to=tx_to)
        )
        await self.session.flush()

    async def close_valid(self, state_id: str, valid_to_beat: int) -> None:
        """Close a belief's valid interval (forgetting, §8.5) — row preserved."""
        await self.session.execute(
            update(BitemporalState)
            .where(BitemporalState.id == state_id)
            .values(valid_to_beat=valid_to_beat)
        )
        await self.session.flush()

    async def history(self, book_id: str, fact_key: str, branch: str) -> list[BitemporalState]:
        """Every belief of one logical fact, oldest tx first (the correction timeline)."""
        stmt = (
            select(BitemporalState)
            .where(
                BitemporalState.book_id == book_id,
                BitemporalState.fact_key == fact_key,
                BitemporalState.branch == branch,
            )
            .order_by(BitemporalState.tx_from, BitemporalState.stamp_wall)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def as_of(
        self,
        book_id: str,
        branch: str,
        beat: int,
        as_of_tx: datetime | None = None,
        *,
        subject_entity_key: str | None = None,
    ) -> list[BitemporalState]:
        """The 4-D time-travel read.

        Returns every fact on ``branch`` that was:
          * valid at ``beat``           — ``valid_from_beat <= beat < valid_to_beat`` (open ⇒ +∞)
          * believed at ``as_of_tx``    — ``tx_from <= as_of_tx < tx_to``  (None ⇒ current belief)

        When two beliefs of the same fact_key both pass (only possible mid-transition), the
        latest CRDT stamp wins, mirroring the LWW resolution of concurrent edits.
        """
        valid_clause = and_(
            BitemporalState.valid_from_beat <= beat,
            or_(
                BitemporalState.valid_to_beat.is_(None),
                BitemporalState.valid_to_beat > beat,
            ),
        )
        tx_clause: ColumnElement[bool]
        if as_of_tx is None:
            tx_clause = BitemporalState.tx_to.is_(None)
        else:
            tx_clause = and_(
                BitemporalState.tx_from <= as_of_tx,
                or_(
                    BitemporalState.tx_to.is_(None),
                    BitemporalState.tx_to > as_of_tx,
                ),
            )
        stmt = select(BitemporalState).where(
            BitemporalState.book_id == book_id,
            BitemporalState.branch == branch,
            valid_clause,
            tx_clause,
        )
        if subject_entity_key is not None:
            stmt = stmt.where(BitemporalState.subject_entity_key == subject_entity_key)
        stmt = stmt.order_by(
            BitemporalState.fact_key,
            BitemporalState.stamp_wall.desc(),
            BitemporalState.stamp_counter.desc(),
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        # Dedup to the winning belief per fact_key (stamp DESC already ordered first).
        seen: set[str] = set()
        out: list[BitemporalState] = []
        for row in rows:
            if row.fact_key in seen:
                continue
            seen.add(row.fact_key)
            out.append(row)
        return out

    async def list_branch(self, book_id: str, branch: str) -> list[BitemporalState]:
        """Every row on a branch (all beliefs) — for diff/merge/branch export."""
        stmt = (
            select(BitemporalState)
            .where(BitemporalState.book_id == book_id, BitemporalState.branch == branch)
            .order_by(BitemporalState.fact_key, BitemporalState.tx_from)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class CanonAuditRepo(BaseRepository):
    """Append + replay the append-only, hash-chained canon audit log (§8)."""

    @staticmethod
    def compute_hash(
        prev_hash: str | None,
        *,
        seq: int,
        action: str,
        actor_id: str,
        target_key: str | None,
        payload_repr: str,
    ) -> str:
        """``H(prev_hash || seq || action || actor || target || payload)`` (sha256 hex)."""
        h = hashlib.sha256()
        h.update((prev_hash or "").encode())
        h.update(f"|{seq}|{action}|{actor_id}|{target_key or ''}|{payload_repr}".encode())
        return h.hexdigest()

    async def head(self, book_id: str) -> CanonAudit | None:
        """The most recent audit row (the chain head we extend)."""
        stmt = (
            select(CanonAudit)
            .where(CanonAudit.book_id == book_id)
            .order_by(CanonAudit.seq.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def next_seq(self, book_id: str) -> int:
        stmt = select(func.coalesce(func.max(CanonAudit.seq), 0)).where(
            CanonAudit.book_id == book_id
        )
        return int((await self.session.execute(stmt)).scalar_one()) + 1

    async def append(
        self,
        *,
        book_id: str,
        branch: str,
        action: AuditAction,
        actor_id: str,
        target_key: str | None,
        payload: dict | None,
        payload_repr: str,
    ) -> CanonAudit:
        """Append one immutable, chained row. Caller holds the branch advisory lock."""
        head = await self.head(book_id)
        seq = (head.seq + 1) if head is not None else 1
        prev_hash = head.entry_hash if head is not None else None
        entry_hash = self.compute_hash(
            prev_hash,
            seq=seq,
            action=action.value,
            actor_id=actor_id,
            target_key=target_key,
            payload_repr=payload_repr,
        )
        row = CanonAudit(
            id=new_id(),
            book_id=book_id,
            seq=seq,
            branch=branch,
            action=action,
            actor_id=actor_id,
            target_key=target_key,
            payload=payload,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def replay(self, book_id: str, limit: int | None = None) -> list[CanonAudit]:
        """Replay the log in sequence order (newest last)."""
        stmt = (
            select(CanonAudit)
            .where(CanonAudit.book_id == book_id)
            .order_by(CanonAudit.seq)
        )
        if limit is not None:
            # Tail: take the last ``limit`` by selecting the highest seqs then re-ordering.
            sub = (
                select(CanonAudit.id)
                .where(CanonAudit.book_id == book_id)
                .order_by(CanonAudit.seq.desc())
                .limit(limit)
            )
            ids = list((await self.session.execute(sub)).scalars().all())
            stmt = select(CanonAudit).where(CanonAudit.id.in_(ids)).order_by(CanonAudit.seq)
        return list((await self.session.execute(stmt)).scalars().all())


class CanonBranchRepo(BaseRepository):
    """The branch registry (FORK / DIFF / MERGE)."""

    async def create(
        self,
        *,
        book_id: str,
        name: str,
        parent: str | None,
        base_beat: int | None,
        base_tx: datetime | None,
        note: str | None = None,
    ) -> CanonBranch:
        row = CanonBranch(
            id=new_id(),
            book_id=book_id,
            name=name,
            parent=parent,
            status=BranchStatus.OPEN,
            base_beat=base_beat,
            base_tx=base_tx,
            note=note,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, book_id: str, name: str) -> CanonBranch | None:
        stmt = select(CanonBranch).where(
            CanonBranch.book_id == book_id, CanonBranch.name == name
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_for_book(self, book_id: str) -> list[CanonBranch]:
        stmt = (
            select(CanonBranch)
            .where(CanonBranch.book_id == book_id)
            .order_by(CanonBranch.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def set_status(self, book_id: str, name: str, status: BranchStatus) -> None:
        await self.session.execute(
            update(CanonBranch)
            .where(CanonBranch.book_id == book_id, CanonBranch.name == name)
            .values(status=status)
        )
        await self.session.flush()


__all__ = ["BitemporalStateRepo", "CanonAuditRepo", "CanonBranchRepo"]
