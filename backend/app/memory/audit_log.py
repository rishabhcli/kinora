"""The append-only, hash-chained canon audit log (kinora.md §8).

Every canon mutation — a bitemporal fact assert/correct/retire, an entity upsert, a branch
fork/merge — is recorded as one immutable row whose ``entry_hash`` covers the *previous*
row's hash plus this row's payload. Re-hashing the chain detects any retroactive edit, so
the log is **tamper-evident**: a judge (or the Continuity Supervisor) can prove the canon's
history was not silently rewritten.

This mirrors the budget ledger's append-only discipline (§11.1) but adds the hash-chain so
the canon's provenance — *who* changed *what* and *when* — is verifiable, not merely stored.
"""

from __future__ import annotations

import json
from typing import Any

from app.db.models.bitemporal import AuditAction, CanonAudit
from app.db.repositories.bitemporal import CanonAuditRepo
from app.memory.contracts import AuditChain, AuditEntry


def _canonical(payload: dict[str, Any] | None) -> str:
    """A deterministic JSON encoding of the payload for hashing (sorted keys, no spaces)."""
    if payload is None:
        return ""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


class AuditLog:
    """Append + replay + verify the canon's hash-chained audit log."""

    def __init__(self, repo: CanonAuditRepo) -> None:
        self._repo = repo

    async def record(
        self,
        *,
        book_id: str,
        branch: str,
        action: AuditAction,
        actor_id: str,
        target_key: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Append one mutation to the chain (the caller holds the branch advisory lock)."""
        row = await self._repo.append(
            book_id=book_id,
            branch=branch,
            action=action,
            actor_id=actor_id,
            target_key=target_key,
            payload=payload,
            payload_repr=_canonical(payload),
        )
        return _to_entry(row)

    async def replay(self, book_id: str, limit: int | None = None) -> AuditChain:
        """Replay the log and verify its hash-chain end-to-end."""
        rows = await self._repo.replay(book_id, limit=None)
        intact, broken_at = self._verify(rows)
        entries = [_to_entry(r) for r in rows]
        if limit is not None:
            entries = entries[-limit:]
        return AuditChain(
            book_id=book_id, entries=entries, intact=intact, broken_at_seq=broken_at
        )

    @staticmethod
    def _verify(rows: list[CanonAudit]) -> tuple[bool, int | None]:
        """Re-hash the chain in sequence; report the first row that fails (if any)."""
        prev_hash: str | None = None
        for row in rows:
            expected = CanonAuditRepo.compute_hash(
                prev_hash,
                seq=row.seq,
                action=row.action.value,
                actor_id=row.actor_id,
                target_key=row.target_key,
                payload_repr=_canonical(row.payload),
            )
            if expected != row.entry_hash or (row.prev_hash or None) != (prev_hash or None):
                return False, row.seq
            prev_hash = row.entry_hash
        return True, None


def _to_entry(row: CanonAudit) -> AuditEntry:
    return AuditEntry(
        id=row.id,
        seq=row.seq,
        book_id=row.book_id,
        branch=row.branch,
        action=row.action.value,
        actor_id=row.actor_id,
        target_key=row.target_key,
        payload=row.payload,
        prev_hash=row.prev_hash,
        entry_hash=row.entry_hash,
        created_at=row.created_at,
    )


__all__ = ["AuditLog"]
