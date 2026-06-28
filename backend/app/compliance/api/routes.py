"""Compliance & consent API routes (mounted under ``/api/compliance``).

The reader-facing surface lets a subject see and change their own consent, file a
DSAR, read their proof trail + ledger slice, and view their compliance report.
The DPO/admin surface (legal holds, DSAR transitions, ledger verification, the
retention schedule) is *additionally* guarded by an admin check.

A :class:`~app.compliance.service.ComplianceService` is built per request from
``container.session_factory`` inside one committing unit of work. Domain errors
are translated to the gateway's typed ``APIError`` here, so the shared
:mod:`app.api.errors` handler stays untouched (additive-only rule).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.compliance.api.schemas import (
    ComplianceReportResponse,
    ConsentHistoryResponse,
    ConsentMutationRequest,
    ConsentRecordView,
    ConsentSnapshotResponse,
    DSARActionRequest,
    DSARListResponse,
    DSARView,
    LedgerEntryView,
    LedgerSliceResponse,
    LedgerVerifyResponse,
    LegalHoldView,
    OpenDSARRequest,
    PlaceHoldRequest,
    PurposeConsentView,
    RetentionRuleView,
    RetentionScheduleResponse,
    RuleResultView,
)
from app.compliance.consent.service import PurposeConsent
from app.compliance.dsar.service import DSARView as DSARDomainView
from app.compliance.errors import ComplianceError
from app.compliance.service import ComplianceService
from app.core.logging import get_logger
from app.db.models.user import User

logger = get_logger("app.compliance.api")

router = APIRouter(prefix="/compliance", tags=["compliance"])

T = TypeVar("T")

#: A demo/dev admin allow-list. In production this would consult a role claim;
#: kept minimal here so the DPO surface is exercisable without a roles subsystem.
_ADMIN_EMAILS = frozenset({"demo@kinora.local"})


async def _run(container: ContainerDep, fn: Callable[[ComplianceService], Awaitable[T]]) -> T:
    """Run ``fn`` against a fresh, committing :class:`ComplianceService` session.

    Domain :class:`ComplianceError`\\ s are translated into the gateway's typed
    ``APIError`` envelope so the client sees a stable ``{error:{type,...}}`` body.
    """
    try:
        async with container.session_factory() as session:
            service = ComplianceService(session)
            return await fn(service)
    except ComplianceError as exc:
        raise APIError(exc.code, exc.message, status=exc.status) from exc


def _require_admin(user: User) -> None:
    """Fail-closed admin gate for the DPO surface (403 otherwise)."""
    if user.email not in _ADMIN_EMAILS:
        raise APIError(
            "forbidden", "compliance administration requires an operator role", status=403
        )


def _consent_view(c: PurposeConsent) -> PurposeConsentView:
    return PurposeConsentView(
        purpose=c.purpose,
        state=c.state,
        granted_version=c.granted_version,
        current_version=c.current_version,
        is_granted=c.is_granted,
        is_stale=c.is_stale,
        decided_at=c.decided_at,
    )


def _dsar_view(v: DSARDomainView) -> DSARView:
    return DSARView(
        id=v.id,
        subject_id=v.subject_id,
        kind=v.kind,
        state=v.state,
        received_at=v.received_at,
        due_at=v.due_at,
        effective_due_at=v.effective_due_at,
        completed_at=v.completed_at,
        overdue=v.overdue,
        result=v.result,
    )


# --------------------------------------------------------------------------- #
# Consent (subject-facing)
# --------------------------------------------------------------------------- #


@router.get("/consent", response_model=ConsentSnapshotResponse)
async def my_consent(container: ContainerDep, user: CurrentUser) -> ConsentSnapshotResponse:
    """The caller's current consent for every purpose."""

    async def _do(service: ComplianceService) -> ConsentSnapshotResponse:
        await service.bootstrap()  # idempotent: ensures active policies exist
        snapshot = await service.consent.snapshot(user.id)
        return ConsentSnapshotResponse(
            subject_id=user.id,
            purposes=[_consent_view(c) for c in snapshot.purposes],
        )

    return await _run(container, _do)


@router.post("/consent/grant", response_model=PurposeConsentView)
async def grant_consent(
    body: ConsentMutationRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> PurposeConsentView:
    """Grant the caller's consent for a purpose (against the active policy version)."""

    async def _do(service: ComplianceService) -> PurposeConsentView:
        await service.bootstrap()
        result = await service.consent.grant(
            subject_id=user.id, purpose=body.purpose, note=body.note
        )
        return _consent_view(result)

    return await _run(container, _do)


@router.post("/consent/withdraw", response_model=PurposeConsentView)
async def withdraw_consent(
    body: ConsentMutationRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> PurposeConsentView:
    """Withdraw the caller's consent for a purpose (as easy as granting, Art. 7(3))."""

    async def _do(service: ComplianceService) -> PurposeConsentView:
        result = await service.consent.withdraw(
            subject_id=user.id, purpose=body.purpose, note=body.note
        )
        return _consent_view(result)

    return await _run(container, _do)


@router.get("/consent/history", response_model=ConsentHistoryResponse)
async def my_consent_history(container: ContainerDep, user: CurrentUser) -> ConsentHistoryResponse:
    """The caller's append-only proof-of-consent trail (Art. 7(1))."""

    async def _do(service: ComplianceService) -> ConsentHistoryResponse:
        records = await service.consent._records.history(user.id)  # noqa: SLF001
        return ConsentHistoryResponse(
            subject_id=user.id,
            records=[
                ConsentRecordView(
                    id=r.id,
                    purpose=r.purpose,
                    action=r.action.value,
                    policy_version=r.policy_version,
                    lawful_basis=r.lawful_basis,
                    created_at=r.created_at,
                    note=r.note,
                )
                for r in records
            ],
        )

    return await _run(container, _do)


# --------------------------------------------------------------------------- #
# DSAR (subject-facing)
# --------------------------------------------------------------------------- #


@router.post("/dsar", response_model=DSARView)
async def open_dsar(
    body: OpenDSARRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> DSARView:
    """File a data-subject-access request for the caller."""

    async def _do(service: ComplianceService) -> DSARView:
        request = await service.dsar.open_request(
            subject_id=user.id, kind=body.kind, subject_email=user.email, note=body.note
        )
        return _dsar_view(service.dsar.view(request))

    return await _run(container, _do)


@router.get("/dsar", response_model=DSARListResponse)
async def my_dsars(container: ContainerDep, user: CurrentUser) -> DSARListResponse:
    """Every DSAR the caller has filed."""

    async def _do(service: ComplianceService) -> DSARListResponse:
        views = await service.dsar.list_for_subject(user.id)
        return DSARListResponse(requests=[_dsar_view(v) for v in views])

    return await _run(container, _do)


@router.post("/dsar/{request_id}/cancel", response_model=DSARView)
async def cancel_dsar(
    request_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> DSARView:
    """Cancel the caller's own DSAR (allowed from any open state)."""

    async def _do(service: ComplianceService) -> DSARView:
        request = await service.dsar._repo.get(request_id)  # noqa: SLF001
        if request is None or request.subject_id != user.id:
            raise APIError("compliance_not_found", "no such DSAR for this user", status=404)
        cancelled = await service.dsar.cancel(request_id, actor_id=user.id)
        return _dsar_view(service.dsar.view(cancelled))

    return await _run(container, _do)


# --------------------------------------------------------------------------- #
# Compliance report (subject-facing)
# --------------------------------------------------------------------------- #


@router.get("/report", response_model=ComplianceReportResponse)
async def my_report(container: ContainerDep, user: CurrentUser) -> ComplianceReportResponse:
    """The caller's consolidated compliance report (policy-as-code evaluation)."""

    async def _do(service: ComplianceService) -> ComplianceReportResponse:
        await service.bootstrap()
        report = await service.report(user.id)
        data = report.to_dict()
        return ComplianceReportResponse(
            subject_id=data["subject_id"],
            generated_at=report.generated_at,
            decision=report.decision,
            is_compliant=data["is_compliant"],
            obligations=data["obligations"],
            summary=data["summary"],
            rules=[RuleResultView(**r) for r in data["rules"]],
        )

    return await _run(container, _do)


# --------------------------------------------------------------------------- #
# Retention schedule (read-only; informational)
# --------------------------------------------------------------------------- #


@router.get("/retention", response_model=RetentionScheduleResponse)
async def retention_schedule(
    container: ContainerDep, user: CurrentUser
) -> RetentionScheduleResponse:
    """The retention schedule (per-data-class TTL + lawful basis)."""

    async def _do(service: ComplianceService) -> RetentionScheduleResponse:
        await service.bootstrap()
        rules = await service.retention._rules.list_all()  # noqa: SLF001
        return RetentionScheduleResponse(
            rules=[
                RetentionRuleView(
                    data_class=r.data_class,
                    ttl_days=r.ttl_days,
                    lawful_basis=r.lawful_basis,
                    expire_on_consent_withdrawal=r.expire_on_consent_withdrawal,
                    description=r.description,
                )
                for r in rules
            ]
        )

    return await _run(container, _do)


# --------------------------------------------------------------------------- #
# DPO / admin surface
# --------------------------------------------------------------------------- #


@router.post("/holds", response_model=LegalHoldView)
async def place_hold(
    body: PlaceHoldRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> LegalHoldView:
    """Place a legal hold over a subject (DPO/operator only)."""
    _require_admin(user)

    async def _do(service: ComplianceService) -> LegalHoldView:
        hold = await service.holds.place(
            subject_id=body.subject_id,
            matter_id=body.matter_id,
            reason=body.reason,
            data_class=body.data_class,
            placed_by=user.id,
        )
        return LegalHoldView(
            id=hold.id,
            subject_id=hold.subject_id,
            data_class=hold.data_class,
            status=hold.status.value,
            matter_id=hold.matter_id,
            reason=hold.reason,
            placed_by=hold.placed_by,
            placed_at=hold.placed_at,
            lifted_at=hold.lifted_at,
        )

    return await _run(container, _do)


@router.post("/holds/{hold_id}/lift", response_model=LegalHoldView)
async def lift_hold(
    hold_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> LegalHoldView:
    """Lift an active legal hold (DPO/operator only)."""
    _require_admin(user)

    async def _do(service: ComplianceService) -> LegalHoldView:
        hold = await service.holds.lift(hold_id, lifted_by=user.id)
        return LegalHoldView(
            id=hold.id,
            subject_id=hold.subject_id,
            data_class=hold.data_class,
            status=hold.status.value,
            matter_id=hold.matter_id,
            reason=hold.reason,
            placed_by=hold.placed_by,
            placed_at=hold.placed_at,
            lifted_at=hold.lifted_at,
        )

    return await _run(container, _do)


@router.post("/dsar/{request_id}/advance", response_model=DSARView)
async def advance_dsar(
    request_id: str,
    body: DSARActionRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
    action: str,
) -> DSARView:
    """Drive a DSAR through its workflow (DPO/operator only).

    ``action`` (query param) is one of ``verify`` / ``begin`` / ``extend`` /
    ``fulfil`` / ``reject``.
    """
    _require_admin(user)

    async def _do(service: ComplianceService) -> DSARView:
        if action == "verify":
            request = await service.dsar.start_verification(request_id, actor_id=user.id)
        elif action == "begin":
            request = await service.dsar.begin(request_id, actor_id=user.id)
        elif action == "extend":
            request = await service.dsar.extend(request_id, actor_id=user.id, reason=body.reason)
        elif action == "fulfil":
            request = await service.dsar.fulfil(request_id, actor_id=user.id)
        elif action == "reject":
            request = await service.dsar.reject(
                request_id, reason=body.reason or "rejected", actor_id=user.id
            )
        else:
            raise APIError("validation_error", f"unknown DSAR action {action!r}", status=422)
        return _dsar_view(service.dsar.view(request))

    return await _run(container, _do)


@router.get("/ledger", response_model=LedgerSliceResponse)
async def my_ledger(container: ContainerDep, user: CurrentUser) -> LedgerSliceResponse:
    """The caller's slice of the consolidated compliance ledger."""

    async def _do(service: ComplianceService) -> LedgerSliceResponse:
        entries = await service.ledger.for_subject(user.id)
        return LedgerSliceResponse(
            subject_id=user.id,
            entries=[
                LedgerEntryView(
                    seq=e.seq,
                    category=e.category,
                    event=e.event,
                    subject_id=e.subject_id,
                    actor_id=e.actor_id,
                    created_at=e.created_at,
                    entry_hash=e.entry_hash,
                    prev_hash=e.prev_hash,
                )
                for e in entries
            ],
        )

    return await _run(container, _do)


@router.get("/ledger/verify", response_model=LedgerVerifyResponse)
async def verify_ledger(container: ContainerDep, user: CurrentUser) -> LedgerVerifyResponse:
    """Re-hash the consolidated ledger chain and report tamper (DPO/operator only)."""
    _require_admin(user)

    async def _do(service: ComplianceService) -> LedgerVerifyResponse:
        result = await service.ledger.verify()
        return LedgerVerifyResponse(
            ok=result.ok,
            entries=result.entries,
            broken_at=result.broken_at,
            reason=result.reason,
        )

    return await _run(container, _do)


__all__ = ["router"]
