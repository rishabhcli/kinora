"""The :class:`AuditService` — record, verify, query, provenance, seal, export.

This is the single orchestration surface over a pluggable :class:`AuditSink`. It
turns a typed :class:`~app.audit.events.AuditEvent` into a tamper-evident,
redaction-aware, hash-chained record and answers the questions an operator /
auditor asks of the trail.

Lifecycle of one append (:meth:`record`):

1. **redact** the event's PII-bearing fields into commitments
   (:class:`~app.audit.redaction.Redactor`) — plaintext never reaches storage;
2. **project** the redacted fields into the canonical hashable core;
3. **chain** ``entry_hash = sha256(prev_hash || canonical_json(core))`` onto the
   current tail;
4. **append** the immutable record; on a lost ``seq`` race, retry against the new
   tail (so concurrent appenders never both claim a slot);
5. **auto-seal** a Merkle checkpoint once the unsealed run reaches the configured
   segment size.

Reads:

* :meth:`verify_integrity` — re-hash the whole chain *and* re-verify every Merkle
  checkpoint root, detecting any insert / edit / delete;
* :meth:`query` / :meth:`count` — declarative search;
* :meth:`provenance_trail` — every event touching one target (a clip, a canon
  fact), correlation-expanded, in chain order — the full story behind an artifact;
* :meth:`accountability_slice` — every event by / about one actor;
* :meth:`forget_subject` — erase a subject's PII in place (commitments preserve
  the chain);
* :meth:`seal_segment` / :meth:`apply_retention` — checkpoint old segments and
  prune entries already covered by a sealed root;
* :meth:`export` — a portable, self-verifying JSON document of the trail.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from app.audit.chain import (
    GENESIS_PREV_HASH,
    ChainCheck,
    _ChainItem,
    canonical_json,
    chain_hash,
    merkle_root,
    recompute_chain,
    sha256_hex,
)
from app.audit.events import AuditEvent
from app.audit.query import AuditQuery
from app.audit.redaction import Redactor
from app.audit.store import (
    AuditRecord,
    AuditSink,
    CheckpointRecord,
    now_utc,
)
from app.core.logging import get_logger

logger = get_logger("app.audit")


class DuplicateSeqError(Exception):
    """Raised by a sink when a ``seq`` slot is already taken (lost the race)."""


# Exceptions that signal a lost (seq) race and should trigger an append retry.
# The in-memory sink raises ``DuplicateSeqError``; a DB sink surfaces the unique
# constraint as SQLAlchemy's ``IntegrityError``.
try:  # pragma: no cover - import guard; sqlalchemy is a core dependency
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    SEQ_RACE_ERRORS: tuple[type[Exception], ...] = (DuplicateSeqError, _IntegrityError)
except ImportError:  # pragma: no cover
    SEQ_RACE_ERRORS = (DuplicateSeqError,)

#: How many times an append retries after losing the (seq) race before failing.
_APPEND_RETRIES = 8


def _new_id() -> str:
    """A 32-char opaque id, mirroring ``app.db.base.new_id`` without importing the DB."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class IntegrityReport:
    """The verdict of a full integrity verification.

    ``ok`` is True iff the hash chain re-derives intact *and* every Merkle
    checkpoint root matches the entries it covers. The sub-results pinpoint the
    failure: ``chain`` for a per-entry tamper, ``checkpoint_broken_at`` for a
    forged / rewritten sealed segment.
    """

    ok: bool
    chain: ChainCheck
    checkpoints_verified: int
    checkpoint_broken_at: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ProvenanceTrail:
    """The reconstructed story behind one target (e.g. a clip)."""

    target_type: str | None
    target_id: str
    events: list[AuditRecord]
    correlation_ids: list[str]


class AuditService:
    """Append-only, hash-chained, redaction-aware audit service over a sink."""

    def __init__(
        self,
        sink: AuditSink,
        *,
        redactor: Redactor | None = None,
        segment_size: int = 128,
        retention: timedelta | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sink = sink
        self._redactor = redactor or Redactor()
        self._segment_size = max(1, segment_size)
        self._retention = retention
        self._clock = clock or now_utc

    # ------------------------------------------------------------------ #
    # Append
    # ------------------------------------------------------------------ #

    async def record(self, event: AuditEvent) -> AuditRecord:
        """Redact, hash, chain, and append one event; returns the stored record.

        Retries on a lost ``seq`` race so concurrent appenders never collide.
        Auto-seals a Merkle checkpoint when a full segment accumulates.
        """
        assert event.severity is not None  # filled by the model validator
        last_error: Exception | None = None
        for _ in range(_APPEND_RETRIES):
            tail = await self._sink.tail()
            seq = (tail.seq + 1) if tail is not None else 1
            prev_hash = tail.entry_hash if tail is not None else GENESIS_PREV_HASH

            event_id = _new_id()
            before = self._redactor.redact(event.before) if event.before is not None else None
            after = self._redactor.redact(event.after) if event.after is not None else None
            payload = self._redactor.redact(event.payload) if event.payload is not None else None
            reason = self._redactor.redact(event.reason) if event.reason is not None else None

            record = AuditRecord(
                id=event_id,
                seq=seq,
                occurred_at=event.occurred_at,
                category=event.category,
                action=event.action,
                severity=event.severity,
                actor_kind=event.actor_kind,
                actor_id=event.actor_id,
                target_type=event.target_type,
                target_id=event.target_id,
                correlation_id=event.correlation_id,
                trace_id=event.trace_id,
                reason=reason,
                before=before,
                after=after,
                payload=payload,
                prev_hash=prev_hash,
                entry_hash="",  # filled next
                created_at=self._clock(),
            )
            entry_hash = chain_hash(prev_hash, record.to_core())
            record = replace(record, entry_hash=entry_hash)
            try:
                stored = await self._sink.append(record)
            except SEQ_RACE_ERRORS as exc:
                # A duplicate-seq is the only retryable condition: another appender
                # claimed this slot first. Roll back (if the sink owns a tx) and
                # retry against the new tail. Any other error propagates.
                last_error = exc
                rollback = getattr(self._sink, "rollback", None)
                if rollback is not None:
                    await rollback()
                logger.debug("audit.seq_race", audit_seq=seq, audit_action=event.action.value)
                continue
            await self._maybe_seal()
            return stored
        assert last_error is not None  # noqa: S101 - loop runs >= 1 time
        raise last_error

    async def record_event(
        self,
        action: Any,
        *,
        actor_kind: Any,
        actor_id: str,
        **kwargs: Any,
    ) -> AuditRecord:
        """Convenience: build an :class:`AuditEvent` from ``action`` and record it."""
        event = AuditEvent.for_action(action, actor_kind=actor_kind, actor_id=actor_id, **kwargs)
        return await self.record(event)

    # ------------------------------------------------------------------ #
    # Verify
    # ------------------------------------------------------------------ #

    async def verify_integrity(self) -> IntegrityReport:
        """Re-hash the whole chain and re-verify every Merkle checkpoint."""
        entries = await self._sink.all_ordered()
        items = [
            _ChainItem(
                seq=r.seq, prev_hash=r.prev_hash, entry_hash=r.entry_hash, core=r.to_core()
            )
            for r in entries
        ]
        chain = recompute_chain(items)
        if not chain.ok:
            return IntegrityReport(
                ok=False, chain=chain, checkpoints_verified=0, reason=chain.reason
            )

        # Re-verify each sealed checkpoint's Merkle root against its segment.
        by_seq = {r.seq: r for r in entries}
        prev_cp_hash = GENESIS_PREV_HASH
        verified = 0
        for cp in await self._sink.all_checkpoints():
            leaves = [
                by_seq[s].entry_hash for s in range(cp.from_seq, cp.to_seq + 1) if s in by_seq
            ]
            # If the segment was pruned post-seal, the leaves are gone but the
            # checkpoint still self-attests via its stored root; only verify the
            # root when the entries are still present.
            segment_intact = len(leaves) == (cp.to_seq - cp.from_seq + 1)
            if segment_intact and merkle_root(leaves) != cp.merkle_root:
                return IntegrityReport(
                    ok=False,
                    chain=chain,
                    checkpoints_verified=verified,
                    checkpoint_broken_at=cp.seq,
                    reason=f"checkpoint {cp.seq} merkle root mismatch",
                )
            expected_cp_hash = _checkpoint_hash(prev_cp_hash, cp)
            if expected_cp_hash != cp.checkpoint_hash:
                return IntegrityReport(
                    ok=False,
                    chain=chain,
                    checkpoints_verified=verified,
                    checkpoint_broken_at=cp.seq,
                    reason=f"checkpoint {cp.seq} hash mismatch",
                )
            prev_cp_hash = cp.checkpoint_hash
            verified += 1
        return IntegrityReport(ok=True, chain=chain, checkpoints_verified=verified)

    # ------------------------------------------------------------------ #
    # Query / provenance
    # ------------------------------------------------------------------ #

    async def query(self, query: AuditQuery) -> list[AuditRecord]:
        """Search the log (ordered + paginated per the query)."""
        return await self._sink.query(query)

    async def count(self, query: AuditQuery) -> int:
        """How many entries match ``query`` (ignoring limit/offset)."""
        return await self._sink.count(query)

    async def provenance_trail(
        self,
        target_id: str,
        *,
        target_type: str | None = None,
        expand_correlations: bool = True,
    ) -> ProvenanceTrail:
        """The full provenance story behind one target (e.g. a clip).

        Starts from every event whose ``target_id`` is ``target_id`` (the direct
        actions on the artifact), then — when ``expand_correlations`` — pulls in
        every event sharing those events' ``correlation_id`` (the render / session
        that produced it), so the trail explains *how* the artifact came to be:
        the canon facts read, the arbitration decisions taken, the budget spent,
        the render accept/degrade — all in chain order.
        """
        direct = await self._sink.query(
            AuditQuery(target_ids=frozenset({target_id}), target_type=target_type)
        )
        correlation_ids = sorted(
            {r.correlation_id for r in direct if r.correlation_id is not None}
        )
        collected: dict[int, AuditRecord] = {r.seq: r for r in direct}
        if expand_correlations:
            for cid in correlation_ids:
                for rec in await self._sink.query(AuditQuery(correlation_id=cid)):
                    collected[rec.seq] = rec
        ordered = [collected[s] for s in sorted(collected)]
        return ProvenanceTrail(
            target_type=target_type,
            target_id=target_id,
            events=ordered,
            correlation_ids=correlation_ids,
        )

    async def accountability_slice(
        self, actor_id: str, *, since: datetime | None = None
    ) -> list[AuditRecord]:
        """Every event performed by one actor (their accountability slice)."""
        return await self._sink.query(
            AuditQuery(actor_ids=frozenset({actor_id}), since=since)
        )

    # ------------------------------------------------------------------ #
    # Redaction after the fact (right to erasure)
    # ------------------------------------------------------------------ #

    async def forget_subject(self, subject_id: str) -> int:
        """Erase a subject's residual PII from existing entries, in place.

        Because PII was already committed (not stored) at append time, "forget"
        is usually a no-op on the payload — but free-text ``reason`` fields and
        any unscrubbed value are re-run through the redactor here. The entry_hash
        is left untouched, so :meth:`verify_integrity` still passes: the chain
        committed to the *redacted* core, and re-redacting an already-redacted (or
        now-redacted) field is idempotent. Returns the count of entries touched.
        """
        affected = await self._sink.query(AuditQuery(actor_ids=frozenset({subject_id})))
        affected += await self._sink.query(AuditQuery(target_ids=frozenset({subject_id})))
        touched = 0
        seen: set[int] = set()
        for rec in affected:
            if rec.seq in seen:
                continue
            seen.add(rec.seq)
            await self._sink.redact_payload(
                rec.seq,
                before=self._redactor.redact(rec.before) if rec.before is not None else None,
                after=self._redactor.redact(rec.after) if rec.after is not None else None,
                payload=self._redactor.redact(rec.payload) if rec.payload is not None else None,
                reason=self._redactor.redact(rec.reason) if rec.reason is not None else None,
            )
            touched += 1
        return touched

    # ------------------------------------------------------------------ #
    # Sealing + retention
    # ------------------------------------------------------------------ #

    async def _maybe_seal(self) -> None:
        """Seal a checkpoint when the unsealed run reaches a full segment."""
        entries = await self._sink.all_ordered()
        unsealed = [r for r in entries if not r.sealed]
        if len(unsealed) >= self._segment_size:
            await self.seal_segment(up_to_seq=unsealed[self._segment_size - 1].seq)

    async def seal_segment(self, *, up_to_seq: int | None = None) -> CheckpointRecord | None:
        """Seal a Merkle checkpoint over the next unsealed segment.

        ``up_to_seq`` caps the segment's last entry (defaults to the current tail
        — seal everything outstanding). Returns the checkpoint, or None when there
        is nothing unsealed to seal.
        """
        entries = await self._sink.all_ordered()
        unsealed = [r for r in entries if not r.sealed]
        if not unsealed:
            return None
        cap = up_to_seq if up_to_seq is not None else unsealed[-1].seq
        segment = [r for r in unsealed if r.seq <= cap]
        if not segment:
            return None
        from_seq = segment[0].seq
        to_seq = segment[-1].seq
        root = merkle_root([r.entry_hash for r in segment])
        prev_cp = await self._sink.latest_checkpoint()
        prev_cp_hash = prev_cp.checkpoint_hash if prev_cp is not None else GENESIS_PREV_HASH
        ordinal = (prev_cp.seq + 1) if prev_cp is not None else 1
        cp = CheckpointRecord(
            id=_new_id(),
            seq=ordinal,
            from_seq=from_seq,
            to_seq=to_seq,
            merkle_root=root,
            prev_checkpoint_hash=prev_cp_hash,
            checkpoint_hash="",
            created_at=self._clock(),
        )
        cp = replace(cp, checkpoint_hash=_checkpoint_hash(prev_cp_hash, cp))
        stored = await self._sink.append_checkpoint(cp)
        await self._sink.mark_sealed(to_seq)
        logger.info(
            "audit.segment_sealed",
            audit_from_seq=from_seq,
            audit_to_seq=to_seq,
            audit_merkle_root=root,
        )
        return stored

    async def apply_retention(self, *, now: datetime | None = None) -> int:
        """Prune sealed entries older than the retention horizon.

        Only entries that are (a) sealed under a Merkle checkpoint and (b) older
        than ``retention`` are removed — the checkpoint's stored root still proves
        they existed and that the surviving chain is contiguous from the pruning
        boundary. No-op when no retention horizon is configured. Returns the count
        pruned.
        """
        if self._retention is None:
            return 0
        cutoff = (now or self._clock()) - self._retention
        entries = await self._sink.all_ordered()
        # Highest seq that is both sealed and older than the cutoff.
        prunable = [r for r in entries if r.sealed and r.occurred_at < cutoff]
        if not prunable:
            return 0
        boundary = prunable[-1].seq + 1  # keep everything from here on
        removed = await self._sink.prune_before(boundary)
        if removed:
            logger.info("audit.retention_pruned", audit_pruned=removed, audit_boundary=boundary)
        return removed

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #

    async def export(self, query: AuditQuery | None = None) -> dict[str, Any]:
        """A portable, self-verifying JSON document of the trail.

        Carries the entries (their cores + chain hashes), the Merkle checkpoints,
        and an integrity verdict, so a recipient can independently re-hash the
        chain and re-check every root without the originating database.
        """
        records = (
            await self._sink.query(query) if query is not None else await self._sink.all_ordered()
        )
        report = await self.verify_integrity()
        checkpoints = await self._sink.all_checkpoints()
        return {
            "schema": "kinora.audit.export/v1",
            "exported_at": self._clock().isoformat(),
            "integrity": {
                "ok": report.ok,
                "entries": report.chain.entries,
                "checkpoints_verified": report.checkpoints_verified,
                "broken_at_seq": report.chain.broken_at_seq,
                "checkpoint_broken_at": report.checkpoint_broken_at,
                "reason": report.reason,
            },
            "entries": [_record_to_dict(r) for r in records],
            "checkpoints": [_checkpoint_to_dict(c) for c in checkpoints],
        }


def _checkpoint_hash(prev_hash: str, cp: CheckpointRecord) -> str:
    """Hash a checkpoint onto the previous one (a chain of checkpoints)."""
    core = {
        "seq": cp.seq,
        "from_seq": cp.from_seq,
        "to_seq": cp.to_seq,
        "merkle_root": cp.merkle_root,
    }
    return sha256_hex(prev_hash + canonical_json(core))


def _record_to_dict(r: AuditRecord) -> dict[str, Any]:
    return {
        "id": r.id,
        "seq": r.seq,
        "occurred_at": r.occurred_at.isoformat(),
        "category": r.category.value,
        "action": r.action.value,
        "severity": r.severity.value,
        "actor_kind": r.actor_kind.value,
        "actor_id": r.actor_id,
        "target_type": r.target_type,
        "target_id": r.target_id,
        "correlation_id": r.correlation_id,
        "trace_id": r.trace_id,
        "reason": r.reason,
        "before": r.before,
        "after": r.after,
        "payload": r.payload,
        "prev_hash": r.prev_hash,
        "entry_hash": r.entry_hash,
        "sealed": r.sealed,
    }


def _checkpoint_to_dict(c: CheckpointRecord) -> dict[str, Any]:
    return {
        "id": c.id,
        "seq": c.seq,
        "from_seq": c.from_seq,
        "to_seq": c.to_seq,
        "merkle_root": c.merkle_root,
        "prev_checkpoint_hash": c.prev_checkpoint_hash,
        "checkpoint_hash": c.checkpoint_hash,
    }


__all__ = [
    "AuditService",
    "IntegrityReport",
    "ProvenanceTrail",
]
