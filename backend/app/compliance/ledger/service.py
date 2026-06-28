"""The consolidated compliance audit ledger service.

One append-only, hash-chained, tamper-evident log that aggregates events from
every compliance category — consent, retention, DSAR, legal-hold, policy — plus
forwarded **security**, **moderation**, and **billing** events. This is the
single accountability surface a DPO or auditor inspects.

Appends are race-safe: the unique ``(seq)`` constraint means two concurrent
appenders cannot both claim the same slot — one commits, the other's flush
raises ``IntegrityError`` and the caller retries against the new tail. The
service exposes that retry loop via :meth:`record` so callers never see it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.compliance.db.models import ComplianceLedgerEntry
from app.compliance.enums import LedgerCategory
from app.compliance.ledger.chain import GENESIS_PREV_HASH, chain_hash, payload_core
from app.compliance.repositories.ledger import ComplianceLedgerRepo
from app.core.logging import get_logger

logger = get_logger("app.compliance.ledger")

#: How many times to retry an append that lost the (seq) race.
_APPEND_RETRIES = 5


@dataclass(frozen=True)
class LedgerVerification:
    """The result of re-hashing the whole ledger chain."""

    ok: bool
    entries: int
    #: ``seq`` of the first entry whose recomputed hash diverges (None == intact).
    broken_at: int | None = None
    reason: str | None = None


class ComplianceLedger:
    """Append-only, hash-chained compliance audit ledger over one session."""

    def __init__(self, repo: ComplianceLedgerRepo) -> None:
        self._repo = repo

    async def record(
        self,
        *,
        category: LedgerCategory,
        event: str,
        subject_id: str | None = None,
        actor_id: str = "system",
        payload: dict[str, Any] | None = None,
    ) -> ComplianceLedgerEntry:
        """Append one entry, retrying on the (seq) race; returns the stored entry."""
        last_error: IntegrityError | None = None
        for _ in range(_APPEND_RETRIES):
            tail = await self._repo.tail()
            seq = (tail.seq + 1) if tail is not None else 1
            prev_hash = tail.entry_hash if tail is not None else GENESIS_PREV_HASH
            core = payload_core(
                seq=seq,
                category=category.value,
                event=event,
                subject_id=subject_id,
                actor_id=actor_id,
                payload=payload,
            )
            entry_hash = chain_hash(prev_hash, core)
            try:
                entry = await self._repo.append(
                    seq=seq,
                    category=category,
                    event=event,
                    entry_hash=entry_hash,
                    prev_hash=prev_hash,
                    subject_id=subject_id,
                    actor_id=actor_id,
                    payload=payload,
                )
                return entry
            except IntegrityError as exc:  # lost the (seq) race — rollback & retry
                last_error = exc
                await self._repo.session.rollback()
                logger.debug(
                    "compliance.ledger.seq_race",
                    ledger_category=category.value,
                    ledger_event=event,
                )
        # Exhausted retries — surface the constraint error so the caller knows.
        assert last_error is not None  # noqa: S101 - loop runs ≥1 time
        raise last_error

    async def verify(self) -> LedgerVerification:
        """Re-hash the whole chain and report the first tamper (if any)."""
        entries = await self._repo.all_ordered()
        prev_hash: str | None = None
        for index, entry in enumerate(entries):
            expected_seq = index + 1
            if entry.seq != expected_seq:
                return LedgerVerification(
                    ok=False,
                    entries=len(entries),
                    broken_at=entry.seq,
                    reason=f"sequence gap: expected {expected_seq}, got {entry.seq}",
                )
            expected_prev = prev_hash if prev_hash is not None else GENESIS_PREV_HASH
            if (entry.prev_hash or GENESIS_PREV_HASH) != expected_prev:
                return LedgerVerification(
                    ok=False,
                    entries=len(entries),
                    broken_at=entry.seq,
                    reason="prev_hash does not match the preceding entry",
                )
            core = payload_core(
                seq=entry.seq,
                category=entry.category.value,
                event=entry.event,
                subject_id=entry.subject_id,
                actor_id=entry.actor_id,
                payload=entry.payload,
            )
            recomputed = chain_hash(entry.prev_hash, core)
            if recomputed != entry.entry_hash:
                return LedgerVerification(
                    ok=False,
                    entries=len(entries),
                    broken_at=entry.seq,
                    reason="entry_hash does not match recomputed payload",
                )
            prev_hash = entry.entry_hash
        return LedgerVerification(ok=True, entries=len(entries))

    async def for_subject(self, subject_id: str) -> list[ComplianceLedgerEntry]:
        """Every ledger entry concerning a subject (their accountability slice)."""
        return await self._repo.for_subject(subject_id)

    async def by_category(
        self, category: LedgerCategory, *, since: datetime | None = None
    ) -> list[ComplianceLedgerEntry]:
        """Entries in one category (optionally since a time)."""
        return await self._repo.by_category(category, since=since)


__all__ = ["ComplianceLedger", "LedgerVerification"]
