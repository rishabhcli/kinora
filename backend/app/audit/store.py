"""The audit sink contract + an in-memory implementation.

The store is the *append-and-query* substrate the :class:`~app.audit.service.AuditService`
sits on. It is deliberately pluggable behind :class:`AuditSink` so the service is
identical whether it is writing to RAM (tests, ephemeral workers) or a database
(production, via :mod:`app.audit.db`):

* :class:`AuditRecord` — the immutable stored shape (the redacted core plus the
  chain fields ``seq`` / ``prev_hash`` / ``entry_hash``, the DB ``id``, and the
  ``created_at`` storage timestamp). It carries a :meth:`to_core` so a verifier
  re-hashes from exactly the fields the chain committed to.
* :class:`AuditSink` — the async protocol: ``tail`` (chain head), ``append`` (one
  immutable row), ``all_ordered`` (verification / export), ``query`` (search),
  ``count``, plus the segment-checkpoint and retention hooks.
* :class:`InMemoryAuditSink` — a complete, deterministic, infra-free
  implementation used by the default test suite and as a reference.

Append is *append-only*: a sink must reject (or the service must retry) a
duplicate ``seq``. The in-memory sink enforces this with an explicit guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.audit.chain import record_core
from app.audit.query import AuditQuery, paginate
from app.audit.taxonomy import (
    AuditAction,
    AuditActorKind,
    AuditCategory,
    AuditSeverity,
)


@dataclass(frozen=True)
class AuditRecord:
    """One immutable, stored audit entry (the redacted, chained projection)."""

    id: str
    seq: int
    occurred_at: datetime
    category: AuditCategory
    action: AuditAction
    severity: AuditSeverity
    actor_kind: AuditActorKind
    actor_id: str
    target_type: str | None
    target_id: str | None
    correlation_id: str | None
    trace_id: str | None
    reason: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    payload: dict[str, Any] | None
    prev_hash: str
    entry_hash: str
    #: Storage-assigned wall-clock timestamp (distinct from logical ``occurred_at``).
    created_at: datetime
    #: True once this entry's segment has been sealed by a Merkle checkpoint.
    sealed: bool = False

    def to_core(self) -> dict[str, Any]:
        """Re-project the hashable core exactly as the chain committed to it."""
        return record_core(
            seq=self.seq,
            event_id=self.id,
            occurred_at=self.occurred_at.isoformat(),
            category=self.category.value,
            action=self.action.value,
            severity=self.severity.value,
            actor_kind=self.actor_kind.value,
            actor_id=self.actor_id,
            target_type=self.target_type,
            target_id=self.target_id,
            correlation_id=self.correlation_id,
            trace_id=self.trace_id,
            reason=self.reason,
            before=self.before,
            after=self.after,
            payload=self.payload,
        )


@dataclass(frozen=True)
class CheckpointRecord:
    """A sealed Merkle checkpoint over a contiguous segment of entries."""

    id: str
    seq: int  # checkpoint ordinal (1-based)
    from_seq: int  # first entry seq in the segment (inclusive)
    to_seq: int  # last entry seq in the segment (inclusive)
    merkle_root: str
    prev_checkpoint_hash: str
    checkpoint_hash: str
    created_at: datetime


class AuditSink(Protocol):
    """The async append-and-query contract every audit store implements."""

    async def tail(self) -> AuditRecord | None:
        """The most-recent entry (whose ``entry_hash`` the next entry chains onto)."""
        ...

    async def append(self, record: AuditRecord) -> AuditRecord:
        """Persist one immutable entry; raise on a duplicate ``seq``."""
        ...

    async def all_ordered(self) -> list[AuditRecord]:
        """Every entry in ``seq`` order (verification / export)."""
        ...

    async def query(self, query: AuditQuery) -> list[AuditRecord]:
        """Entries matching ``query`` (ordered + paginated per the query)."""
        ...

    async def count(self, query: AuditQuery) -> int:
        """How many entries match ``query`` (ignoring its limit/offset)."""
        ...

    async def latest_checkpoint(self) -> CheckpointRecord | None:
        """The most-recent sealed Merkle checkpoint (None if none sealed)."""
        ...

    async def append_checkpoint(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        """Persist one Merkle checkpoint."""
        ...

    async def all_checkpoints(self) -> list[CheckpointRecord]:
        """Every checkpoint in ordinal order."""
        ...

    async def mark_sealed(self, up_to_seq: int) -> int:
        """Flag every entry with ``seq <= up_to_seq`` sealed; return how many."""
        ...

    async def redact_payload(
        self,
        seq: int,
        *,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        payload: dict[str, Any] | None,
        reason: str | None,
    ) -> bool:
        """Overwrite an entry's PII-bearing fields in place (hash is preserved).

        Used by erasure-after-the-fact: the *commitment* digest stored at append
        time keeps the chain valid, so the plaintext can be replaced with the
        already-redacted projection without re-hashing. Returns True if found.
        """
        ...

    async def prune_before(self, before_seq: int) -> int:
        """Delete sealed entries with ``seq < before_seq``; return how many.

        Pruning is only legitimate for entries already covered by a sealed
        checkpoint (the Merkle root still proves they existed). The service
        enforces that precondition; the sink only performs the delete.
        """
        ...


@dataclass
class InMemoryAuditSink:
    """A complete, deterministic, infra-free :class:`AuditSink`.

    Backs the default test suite and serves as the reference implementation: the
    service behaviour is identical here and against a DB sink.
    """

    _entries: list[AuditRecord] = field(default_factory=list)
    _checkpoints: list[CheckpointRecord] = field(default_factory=list)
    _seqs: set[int] = field(default_factory=set)

    async def tail(self) -> AuditRecord | None:
        return self._entries[-1] if self._entries else None

    async def append(self, record: AuditRecord) -> AuditRecord:
        if record.seq in self._seqs:
            from app.audit.service import DuplicateSeqError

            raise DuplicateSeqError(f"duplicate audit seq {record.seq}")
        self._seqs.add(record.seq)
        self._entries.append(record)
        return record

    async def all_ordered(self) -> list[AuditRecord]:
        return sorted(self._entries, key=lambda r: r.seq)

    async def query(self, query: AuditQuery) -> list[AuditRecord]:
        matched = [r for r in self._entries if query.matches(r)]
        return paginate(matched, query)

    async def count(self, query: AuditQuery) -> int:
        return sum(1 for r in self._entries if query.matches(r))

    async def latest_checkpoint(self) -> CheckpointRecord | None:
        return self._checkpoints[-1] if self._checkpoints else None

    async def append_checkpoint(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        self._checkpoints.append(checkpoint)
        return checkpoint

    async def all_checkpoints(self) -> list[CheckpointRecord]:
        return list(self._checkpoints)

    async def mark_sealed(self, up_to_seq: int) -> int:
        sealed = 0
        for index, rec in enumerate(self._entries):
            if rec.seq <= up_to_seq and not rec.sealed:
                self._entries[index] = _with(rec, sealed=True)
                sealed += 1
        return sealed

    async def redact_payload(
        self,
        seq: int,
        *,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        payload: dict[str, Any] | None,
        reason: str | None,
    ) -> bool:
        for index, rec in enumerate(self._entries):
            if rec.seq == seq:
                self._entries[index] = _with(
                    rec, before=before, after=after, payload=payload, reason=reason
                )
                return True
        return False

    async def prune_before(self, before_seq: int) -> int:
        keep = [r for r in self._entries if r.seq >= before_seq]
        removed = len(self._entries) - len(keep)
        self._entries = keep
        self._seqs = {r.seq for r in keep}
        return removed


def _with(record: AuditRecord, **changes: Any) -> AuditRecord:
    """Return a copy of an (immutable) :class:`AuditRecord` with fields replaced."""
    from dataclasses import replace

    return replace(record, **changes)


def now_utc() -> datetime:
    """Storage wall-clock timestamp helper (timezone-aware UTC)."""
    return datetime.now(UTC)


__all__ = [
    "AuditRecord",
    "AuditSink",
    "CheckpointRecord",
    "InMemoryAuditSink",
    "now_utc",
]
