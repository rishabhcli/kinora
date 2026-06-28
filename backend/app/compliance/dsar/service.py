"""DSAR workflow orchestration.

Drives a data-subject-access-request through its state machine, computes the
statutory deadlines, refuses erasure under a legal hold, and delegates the actual
data work (export bundle / erasure) to an injected :class:`Fulfiller` seam — so
this domain *orchestrates* without re-implementing the ``dataportability``
export/erasure mechanics it complements.

GDPR deadlines (Art. 12(3)):

* ``due_at`` = received + one month (we use 30 days);
* a single extension adds two more months (60 days), recorded as the ``extended``
  state with ``extended_due_at``.

Every transition appends a :class:`~app.compliance.db.models.DSAREvent` and a
consolidated-ledger entry, so the request's history is doubly auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.compliance.clock import Clock, system_clock
from app.compliance.db.models import DSARRequest
from app.compliance.dsar.machine import DSARMachine, is_terminal
from app.compliance.enums import DSARKind, DSARState, LedgerCategory
from app.compliance.errors import ConflictError, LegalHoldError, NotFoundError
from app.compliance.hold.service import LegalHoldService
from app.compliance.ledger.service import ComplianceLedger
from app.compliance.repositories.dsar import DSARRepo
from app.core.logging import get_logger

logger = get_logger("app.compliance.dsar")

#: Statutory response window (GDPR Art. 12(3): "one month").
_RESPONSE_DAYS = 30
#: The one-time extension ("by two further months").
_EXTENSION_DAYS = 60


@dataclass(frozen=True)
class FulfilmentResult:
    """What a :class:`Fulfiller` returns after doing the data work."""

    #: A machine-readable summary of what was produced/erased.
    summary: dict[str, Any]
    #: Optional reference to the export bundle (object-store key / URL).
    artifact_ref: str | None = None


class Fulfiller(Protocol):
    """The seam that actually executes a DSAR (export / erasure / rectification).

    Implemented by the ``dataportability`` domain (or a test fake). The DSAR
    service calls :meth:`fulfil` once a request reaches ``in_progress`` and is
    cleared of legal holds; the returned summary is stored on the request.
    """

    async def fulfil(self, request: DSARRequest) -> FulfilmentResult:
        """Execute the request and return a summary of what was done."""
        ...


class _NullFulfiller:
    """Default fulfiller — records intent without touching real data.

    Used when no ``dataportability`` fulfiller is wired (e.g. tests, or before
    the sibling domain lands). It marks the request as fulfilled with an explicit
    ``"pending_integration"`` summary so the workflow is exercisable end-to-end.
    """

    async def fulfil(self, request: DSARRequest) -> FulfilmentResult:
        return FulfilmentResult(
            summary={"kind": request.kind.value, "status": "pending_integration"}
        )


@dataclass(frozen=True)
class DSARView:
    """A read-only projection of a DSAR for API/report surfaces."""

    id: str
    subject_id: str | None
    kind: DSARKind
    state: DSARState
    received_at: datetime
    due_at: datetime
    effective_due_at: datetime
    completed_at: datetime | None
    overdue: bool
    result: dict[str, Any] | None


class DSARService:
    """Create and drive DSARs through their lifecycle."""

    def __init__(
        self,
        repo: DSARRepo,
        ledger: ComplianceLedger,
        holds: LegalHoldService,
        *,
        clock: Clock = system_clock,
        fulfiller: Fulfiller | None = None,
    ) -> None:
        self._repo = repo
        self._ledger = ledger
        self._holds = holds
        self._clock = clock
        self._fulfiller: Fulfiller = fulfiller or _NullFulfiller()

    # --- creation ----------------------------------------------------------- #

    async def open_request(
        self,
        *,
        subject_id: str,
        kind: DSARKind,
        subject_email: str | None = None,
        note: str | None = None,
        actor_id: str | None = None,
    ) -> DSARRequest:
        """File a new DSAR (RECEIVED), computing its one-month deadline."""
        now = self._clock()
        request = await self._repo.create(
            subject_id=subject_id,
            kind=kind,
            received_at=now,
            due_at=now + timedelta(days=_RESPONSE_DAYS),
            subject_email=subject_email,
            note=note,
        )
        await self._repo.append_event(
            request_id=request.id,
            from_state=None,
            to_state=DSARState.RECEIVED,
            actor_id=actor_id or subject_id,
            detail={"kind": kind.value},
        )
        await self._ledger.record(
            category=LedgerCategory.DSAR,
            event="dsar.received",
            subject_id=subject_id,
            actor_id=actor_id or subject_id,
            payload={"request_id": request.id, "kind": kind.value},
        )
        return request

    # --- transitions -------------------------------------------------------- #

    async def _transition(
        self,
        request_id: str,
        target: DSARState,
        *,
        actor_id: str,
        detail: dict[str, Any] | None = None,
        completed_at: datetime | None = None,
        extended_due_at: datetime | None = None,
        result: dict[str, Any] | None = None,
    ) -> DSARRequest:
        request = await self._repo.get(request_id)
        if request is None:
            raise NotFoundError(f"DSAR {request_id!r} not found")
        DSARMachine.assert_transition(request.state, target)
        previous = request.state
        await self._repo.save_state(
            request,
            state=target,
            completed_at=completed_at,
            extended_due_at=extended_due_at,
            result=result,
        )
        await self._repo.append_event(
            request_id=request.id,
            from_state=previous,
            to_state=target,
            actor_id=actor_id,
            detail=detail,
        )
        await self._ledger.record(
            category=LedgerCategory.DSAR,
            event=f"dsar.{target.value}",
            subject_id=request.subject_id,
            actor_id=actor_id,
            payload={"request_id": request.id, "from": previous.value, "to": target.value},
        )
        return request

    async def start_verification(self, request_id: str, *, actor_id: str = "system") -> DSARRequest:
        """RECEIVED → VERIFYING (identity-verification step)."""
        return await self._transition(request_id, DSARState.VERIFYING, actor_id=actor_id)

    async def begin(self, request_id: str, *, actor_id: str = "system") -> DSARRequest:
        """VERIFYING → IN_PROGRESS (identity verified, work begins)."""
        return await self._transition(request_id, DSARState.IN_PROGRESS, actor_id=actor_id)

    async def extend(
        self, request_id: str, *, actor_id: str = "system", reason: str | None = None
    ) -> DSARRequest:
        """IN_PROGRESS → EXTENDED (the one-time Art. 12(3) two-month extension)."""
        request = await self._repo.get(request_id)
        if request is None:
            raise NotFoundError(f"DSAR {request_id!r} not found")
        if request.extended_due_at is not None:
            raise ConflictError("DSAR has already been extended once")
        extended_due = request.due_at + timedelta(days=_EXTENSION_DAYS)
        return await self._transition(
            request_id,
            DSARState.EXTENDED,
            actor_id=actor_id,
            extended_due_at=extended_due,
            detail={"reason": reason} if reason else None,
        )

    async def reject(
        self, request_id: str, *, reason: str, actor_id: str = "system"
    ) -> DSARRequest:
        """Reject a non-terminal DSAR (e.g. identity could not be verified)."""
        return await self._transition(
            request_id,
            DSARState.REJECTED,
            actor_id=actor_id,
            detail={"reason": reason},
            result={"rejected_reason": reason},
        )

    async def cancel(self, request_id: str, *, actor_id: str | None = None) -> DSARRequest:
        """Cancel a DSAR at the subject's request (allowed from any open state)."""
        request = await self._repo.get(request_id)
        if request is None:
            raise NotFoundError(f"DSAR {request_id!r} not found")
        return await self._transition(
            request_id,
            DSARState.CANCELLED,
            actor_id=actor_id or (request.subject_id or "system"),
        )

    async def fulfil(self, request_id: str, *, actor_id: str = "system") -> DSARRequest:
        """Execute an IN_PROGRESS/EXTENDED DSAR and mark it COMPLETED.

        Erasure-class requests are refused while the subject is under a legal hold
        (the hold suspends erasure); access/portability requests are unaffected.
        """
        request = await self._repo.get(request_id)
        if request is None:
            raise NotFoundError(f"DSAR {request_id!r} not found")
        if request.state not in (DSARState.IN_PROGRESS, DSARState.EXTENDED):
            raise ConflictError(
                f"DSAR must be in_progress/extended to fulfil (is {request.state.value!r})"
            )
        if request.kind == DSARKind.ERASURE and request.subject_id is not None:
            scope = await self._holds.scope(request.subject_id)
            if scope.any_active:
                raise LegalHoldError(request.subject_id, scope.hold_ids[0])

        result = await self._fulfiller.fulfil(request)
        completed = await self._transition(
            request_id,
            DSARState.COMPLETED,
            actor_id=actor_id,
            completed_at=self._clock(),
            result={"summary": result.summary, "artifact_ref": result.artifact_ref},
            detail={"artifact_ref": result.artifact_ref},
        )
        logger.info(
            "compliance.dsar.completed",
            request_id=request_id,
            kind=request.kind.value,
            subject_id=request.subject_id,
        )
        return completed

    # --- reads -------------------------------------------------------------- #

    def view(self, request: DSARRequest) -> DSARView:
        """Project a DSAR row into a read-only view with the effective deadline."""
        effective_due = request.extended_due_at or request.due_at
        overdue = (not is_terminal(request.state)) and effective_due < self._clock()
        return DSARView(
            id=request.id,
            subject_id=request.subject_id,
            kind=request.kind,
            state=request.state,
            received_at=request.received_at,
            due_at=request.due_at,
            effective_due_at=effective_due,
            completed_at=request.completed_at,
            overdue=overdue,
            result=request.result,
        )

    async def get_view(self, request_id: str) -> DSARView:
        """Fetch and project one DSAR (404 if missing)."""
        request = await self._repo.get(request_id)
        if request is None:
            raise NotFoundError(f"DSAR {request_id!r} not found")
        return self.view(request)

    async def list_for_subject(self, subject_id: str) -> list[DSARView]:
        """Every DSAR a subject has filed, newest first."""
        return [self.view(r) for r in await self._repo.list_for_subject(subject_id)]

    async def overdue(self) -> list[DSARView]:
        """Open DSARs past their effective deadline (the SLA-breach watchlist)."""
        rows = await self._repo.list_overdue(self._clock())
        return [self.view(r) for r in rows]


__all__ = ["DSARService", "DSARView", "Fulfiller", "FulfilmentResult"]
