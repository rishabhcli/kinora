"""The bitemporal continuity-fact engine (kinora.md §8.1, §8.5).

This is the bitemporal upgrade of :class:`app.memory.canon_service`'s state handling. Where
the legacy ``continuity_states`` carries valid-time only, every fact here carries **both**
intervals plus a branch and a CRDT write-stamp:

* **assert** — introduce a fact valid from a beat. Stamps it (HLC + actor), opens its
  transaction interval, and records an audit-log entry.
* **correct** — the system changed its mind (a director edit, a Critic conflict resolution
  §9.5). The prior belief's ``tx_to`` is closed *now* and a successor row is inserted with
  the same ``fact_key`` — so the old belief is never destroyed, only retired in tx-time.
* **retire** — §8.5 forgetting: close ``valid_to_beat`` so the fact drops out of forward
  generation but survives for backward / time-travel reads.
* **as_of** — the 4-D read: facts on a branch, valid at a beat, *as the canon believed them*
  at a transaction instant.

The CRDT stamp is what makes concurrent edits conflict-free: two ``correct`` calls from
different actors resolve deterministically to the higher stamp on every replica (LWW), and
``as_of`` dedups to that winner. The HLC keeps stamps monotone and causality-respecting.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.db.models.bitemporal import AuditAction, BitemporalState
from app.db.repositories.bitemporal import BitemporalStateRepo, CanonAuditRepo
from app.memory.audit_log import AuditLog
from app.memory.bitemporal import MAIN_BRANCH, BeatInterval, TxInterval, utcnow
from app.memory.contracts import BeatSpan, BitemporalFact, FactHistory, TxSpan, WriteStamp
from app.memory.crdt import HLC, HLCClock, Stamp


class FactNotFoundError(LookupError):
    """Raised when a correction / retire targets a fact_key with no current belief."""


def _default_clock(actor_id: str) -> HLCClock:
    """An HLC clock anchored to the real wall clock (ms)."""
    return HLCClock(actor_id, now_ms=lambda: int(utcnow().timestamp() * 1000))


class TemporalStateService:
    """Assert / correct / retire bitemporal facts and read them in 4-D."""

    def __init__(
        self,
        states: BitemporalStateRepo,
        audit: AuditLog,
        *,
        actor_id: str = "system",
        clock_factory: Callable[[str], HLCClock] = _default_clock,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._states = states
        self._audit = audit
        self._actor = actor_id
        self._clock = clock_factory(actor_id)
        self._now = now

    # --- writes ------------------------------------------------------------- #

    async def assert_fact(
        self,
        *,
        book_id: str,
        subject_entity_key: str,
        predicate: str,
        object_value: str,
        valid_from_beat: int,
        branch: str = MAIN_BRANCH,
        fact_key: str | None = None,
        source_span: dict[str, Any] | None = None,
    ) -> BitemporalFact:
        """Introduce a fact valid from ``valid_from_beat`` (open-ended). Audited + stamped."""
        await self._states.advisory_lock(book_id, branch)
        key = fact_key or _fact_key(subject_entity_key, predicate, object_value, valid_from_beat)
        stamp = self._clock.issue()
        now = self._now()
        row = await self._states.insert(
            book_id=book_id,
            fact_key=key,
            branch=branch,
            subject_entity_key=subject_entity_key,
            predicate=predicate,
            object_value=object_value,
            valid_from_beat=valid_from_beat,
            valid_to_beat=None,
            tx_from=now,
            stamp_wall=stamp.hlc.wall,
            stamp_counter=stamp.hlc.counter,
            actor_id=self._actor,
            source_span=source_span,
        )
        await self._audit.record(
            book_id=book_id,
            branch=branch,
            action=AuditAction.ASSERT_FACT,
            actor_id=self._actor,
            target_key=key,
            payload={
                "subject": subject_entity_key,
                "predicate": predicate,
                "object": object_value,
                "valid_from_beat": valid_from_beat,
            },
        )
        return _to_fact(row)

    async def correct_fact(
        self,
        *,
        book_id: str,
        fact_key: str,
        new_object: str,
        branch: str = MAIN_BRANCH,
        new_valid_from_beat: int | None = None,
        source_span: dict[str, Any] | None = None,
    ) -> BitemporalFact:
        """Change a belief: close the current row's tx interval, insert a successor.

        The successor shares ``fact_key`` and carries a strictly-greater CRDT stamp (the HLC
        advances), so concurrent corrections from other actors order deterministically.
        """
        await self._states.advisory_lock(book_id, branch)
        current = await self._states.current_belief(book_id, fact_key, branch)
        if current is None:
            raise FactNotFoundError(f"no current belief for fact_key={fact_key} on {branch}")
        now = self._now()
        # Causality: advance our clock past the row we are superseding.
        self._clock.observe(Stamp(HLC(current.stamp_wall, current.stamp_counter), current.actor_id))
        stamp = self._clock.issue()
        await self._states.close_tx(current.id, now)
        successor = await self._states.insert(
            book_id=book_id,
            fact_key=fact_key,
            branch=branch,
            subject_entity_key=current.subject_entity_key,
            predicate=current.predicate,
            object_value=new_object,
            valid_from_beat=(
                current.valid_from_beat if new_valid_from_beat is None else new_valid_from_beat
            ),
            valid_to_beat=current.valid_to_beat,
            tx_from=now,
            stamp_wall=stamp.hlc.wall,
            stamp_counter=stamp.hlc.counter,
            actor_id=self._actor,
            source_span=source_span if source_span is not None else current.source_span,
        )
        await self._audit.record(
            book_id=book_id,
            branch=branch,
            action=AuditAction.CORRECT_FACT,
            actor_id=self._actor,
            target_key=fact_key,
            payload={"from": current.object_value, "to": new_object},
        )
        return _to_fact(successor)

    async def retire_fact(
        self, *, book_id: str, fact_key: str, valid_to_beat: int, branch: str = MAIN_BRANCH
    ) -> BitemporalFact:
        """Forgetting (§8.5): close the fact's valid interval at ``valid_to_beat``."""
        await self._states.advisory_lock(book_id, branch)
        current = await self._states.current_belief(book_id, fact_key, branch)
        if current is None:
            raise FactNotFoundError(f"no current belief for fact_key={fact_key} on {branch}")
        await self._states.close_valid(current.id, valid_to_beat)
        await self._audit.record(
            book_id=book_id,
            branch=branch,
            action=AuditAction.RETIRE_FACT,
            actor_id=self._actor,
            target_key=fact_key,
            payload={"valid_to_beat": valid_to_beat},
        )
        refreshed = await self._states.get(current.id)
        assert refreshed is not None
        return _to_fact(refreshed)

    # --- reads -------------------------------------------------------------- #

    async def as_of(
        self,
        *,
        book_id: str,
        beat: int,
        as_of_tx: datetime | None = None,
        branch: str = MAIN_BRANCH,
        subject_entity_key: str | None = None,
    ) -> list[BitemporalFact]:
        """The 4-D time-travel read (active facts on ``branch`` at ``beat`` as of ``tx``)."""
        rows = await self._states.as_of(
            book_id, branch, beat, as_of_tx, subject_entity_key=subject_entity_key
        )
        return [_to_fact(r) for r in rows]

    async def history(
        self, *, book_id: str, fact_key: str, branch: str = MAIN_BRANCH
    ) -> FactHistory:
        """The full transaction-time history of one logical fact (every past belief)."""
        rows = await self._states.history(book_id, fact_key, branch)
        return FactHistory(
            fact_key=fact_key,
            book_id=book_id,
            branch=branch,
            beliefs=[_to_fact(r) for r in rows],
        )


def _fact_key(subject: str, predicate: str, object_value: str, valid_from_beat: int) -> str:
    """A deterministic, human-readable logical-fact key (stable across corrections).

    Includes the introduction beat so re-asserting the *same* triple at a later beat is a
    distinct logical fact (e.g. "possesses sword" lost then regained), while a correction of
    the *object* keeps the same key (it targets fact_key explicitly).
    """
    import hashlib

    raw = f"{subject}|{predicate}|{object_value}|{valid_from_beat}".encode()
    digest = hashlib.sha1(raw).hexdigest()[:12]
    return f"fact_{subject}_{predicate}_{digest}"


def _to_fact(row: BitemporalState) -> BitemporalFact:
    return BitemporalFact(
        id=row.id,
        fact_key=row.fact_key,
        branch=row.branch,
        subject_entity_key=row.subject_entity_key,
        predicate=row.predicate,
        object_value=row.object_value,
        valid=BeatSpan(valid_from_beat=row.valid_from_beat, valid_to_beat=row.valid_to_beat),
        tx=TxSpan(tx_from=row.tx_from, tx_to=row.tx_to),
        stamp=WriteStamp(
            wall=row.stamp_wall, counter=row.stamp_counter, actor_id=row.actor_id
        ),
        source_span=row.source_span,
        current=row.tx_to is None,
    )


def build_temporal_service(session: Any, *, actor_id: str = "system") -> TemporalStateService:
    """Construct the service over a session (convenience for the MCP tool path)."""
    states = BitemporalStateRepo(session)
    audit = AuditLog(CanonAuditRepo(session))
    return TemporalStateService(states, audit, actor_id=actor_id)


# Re-exported for callers that want the interval algebra alongside the service.
__all__ = [
    "BeatInterval",
    "FactNotFoundError",
    "TemporalStateService",
    "TxInterval",
    "build_temporal_service",
]
