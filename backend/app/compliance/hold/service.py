"""Legal-hold management.

A legal hold suspends retention and erasure for a data subject while litigation
or a regulatory matter is open. A hold may be scoped to a single data class or
cover *all* of the subject's data. The retention engine and the DSAR erasure path
both consult :meth:`LegalHoldService.scope` before expiring/erasing anything.

Every place/lift is mirrored into the consolidated compliance ledger.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.compliance.clock import Clock, system_clock
from app.compliance.db.models import LegalHold
from app.compliance.enums import DataClass, LedgerCategory
from app.compliance.errors import ConflictError, NotFoundError
from app.compliance.ledger.service import ComplianceLedger
from app.compliance.repositories.hold import LegalHoldRepo
from app.core.logging import get_logger

logger = get_logger("app.compliance.hold")


@dataclass(frozen=True)
class HoldScope:
    """The effective legal-hold coverage for a subject (folded over active holds)."""

    subject_id: str
    #: True when at least one active hold covers ALL of the subject's data.
    all_data: bool
    #: Specific data classes individually held (in addition to ``all_data``).
    held_classes: frozenset[DataClass]
    #: The ids of the active holds that produced this scope.
    hold_ids: tuple[str, ...]

    @property
    def any_active(self) -> bool:
        """True when the subject is under any active hold at all."""
        return self.all_data or bool(self.held_classes) or bool(self.hold_ids)

    def covers(self, data_class: DataClass) -> bool:
        """True when erasure/expiry of ``data_class`` is suspended for this subject."""
        return self.all_data or data_class in self.held_classes


class LegalHoldService:
    """Place, lift, and evaluate legal holds."""

    def __init__(
        self,
        repo: LegalHoldRepo,
        ledger: ComplianceLedger,
        *,
        clock: Clock = system_clock,
    ) -> None:
        self._repo = repo
        self._ledger = ledger
        self._clock = clock

    async def place(
        self,
        *,
        subject_id: str,
        matter_id: str,
        reason: str,
        data_class: DataClass | None = None,
        placed_by: str = "system",
    ) -> LegalHold:
        """Place an active hold over a subject (optionally one data class)."""
        hold = await self._repo.place(
            subject_id=subject_id,
            matter_id=matter_id,
            reason=reason,
            data_class=data_class,
            placed_by=placed_by,
            placed_at=self._clock(),
        )
        await self._ledger.record(
            category=LedgerCategory.LEGAL_HOLD,
            event="legal_hold.placed",
            subject_id=subject_id,
            actor_id=placed_by,
            payload={
                "hold_id": hold.id,
                "matter_id": matter_id,
                "data_class": data_class.value if data_class else None,
            },
        )
        logger.info(
            "compliance.hold.placed", subject_id=subject_id, matter_id=matter_id, hold_id=hold.id
        )
        return hold

    async def lift(self, hold_id: str, *, lifted_by: str = "system") -> LegalHold:
        """Lift an active hold; raise if it does not exist or is already lifted."""
        hold = await self._repo.get(hold_id)
        if hold is None:
            raise NotFoundError(f"legal hold {hold_id!r} not found")
        if hold.status.value == "lifted":
            raise ConflictError(f"legal hold {hold_id!r} is already lifted")
        updated = await self._repo.lift(hold_id, lifted_by=lifted_by, lifted_at=self._clock())
        assert updated is not None  # noqa: S101 - existence checked above
        await self._ledger.record(
            category=LedgerCategory.LEGAL_HOLD,
            event="legal_hold.lifted",
            subject_id=updated.subject_id,
            actor_id=lifted_by,
            payload={"hold_id": hold_id, "matter_id": updated.matter_id},
        )
        return updated

    async def scope(self, subject_id: str) -> HoldScope:
        """Fold the subject's active holds into an effective coverage scope."""
        holds = await self._repo.active_for_subject(subject_id)
        all_data = any(h.data_class is None for h in holds)
        held = frozenset(h.data_class for h in holds if h.data_class is not None)
        return HoldScope(
            subject_id=subject_id,
            all_data=all_data,
            held_classes=held,
            hold_ids=tuple(h.id for h in holds),
        )

    async def is_held(self, subject_id: str, data_class: DataClass | None = None) -> bool:
        """True when the subject (or a specific class of theirs) is under hold."""
        scope = await self.scope(subject_id)
        if data_class is None:
            return scope.any_active
        return scope.covers(data_class)


__all__ = ["HoldScope", "LegalHoldService"]
