"""Integration tests for the compliance subsystem (isolated Postgres only).

These exercise the full service stack against a throwaway database — consent
folding + proof trail, the consolidated hash-chained ledger + verification, the
retention engine's consent/hold-aware expiry, the DSAR lifecycle + deadlines, and
legal holds suspending erasure.

They need **only** Postgres (not Redis/MinIO), so they run whenever
``KINORA_TEST_DATABASE_URL`` is set (e.g. the isolated ``kinora_compliance_test``
DB on :5433) and skip cleanly otherwise — matching the repo's infra gating.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.compliance.clock import FixedClock
from app.compliance.consent.policy import PolicyDraft
from app.compliance.db.models import ConsentRecord
from app.compliance.dsar.service import FulfilmentResult
from app.compliance.enums import (
    ConsentState,
    DataClass,
    DSARKind,
    DSARState,
    LedgerCategory,
    ProcessingPurpose,
)
from app.compliance.errors import (
    ConflictError,
    ConsentRequiredError,
    InvalidTransitionError,
    LegalHoldError,
    NotFoundError,
)
from app.compliance.retention.engine import RetentionItem
from app.compliance.service import ComplianceService
from app.db.base import Base, new_id
from app.db.models.user import User

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not _DB_URL,
    reason="compliance integration tests require KINORA_TEST_DATABASE_URL "
    "(e.g. the isolated kinora_compliance_test DB on :5433)",
)

pytestmark = requires_db


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A committing session over a freshly-truncated throwaway database."""
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with maker() as sess:
        yield sess
        await sess.commit()
    await engine.dispose()


async def _make_user(session: AsyncSession, email: str = "subject@example.com") -> str:
    user = User(id=new_id(), email=email, hashed_password="x")
    session.add(user)
    await session.flush()
    return user.id


def _svc(
    session: AsyncSession, clock: FixedClock | None = None, fulfiller: object | None = None
) -> ComplianceService:
    return ComplianceService(
        session, clock=clock or FixedClock(), fulfiller=fulfiller  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Consent
# --------------------------------------------------------------------------- #


async def test_bootstrap_is_idempotent(session: AsyncSession) -> None:
    svc = _svc(session)
    await svc.bootstrap()
    await svc.bootstrap()  # second call must not duplicate active policies
    active = await svc.consent._policies.list_active()  # noqa: SLF001
    purposes = [p.purpose for p in active]
    assert len(purposes) == len(set(purposes))  # one active per purpose
    assert ProcessingPurpose.ADAPTATION in purposes


async def test_grant_then_withdraw_folds_state(session: AsyncSession) -> None:
    svc = _svc(session)
    await svc.bootstrap()
    uid = await _make_user(session)

    granted = await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.ANALYTICS)
    assert granted.state == ConsentState.GRANTED
    assert await svc.consent.has_consent(uid, ProcessingPurpose.ANALYTICS)

    withdrawn = await svc.consent.withdraw(subject_id=uid, purpose=ProcessingPurpose.ANALYTICS)
    assert withdrawn.state == ConsentState.WITHDRAWN
    assert not await svc.consent.has_consent(uid, ProcessingPurpose.ANALYTICS)


async def test_consent_history_is_append_only_proof(session: AsyncSession) -> None:
    svc = _svc(session)
    await svc.bootstrap()
    uid = await _make_user(session)
    await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.ANALYTICS)
    await svc.consent.withdraw(subject_id=uid, purpose=ProcessingPurpose.ANALYTICS)
    await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.ANALYTICS)

    history = await svc.consent._records.history(uid)  # noqa: SLF001
    assert [r.action.value for r in history] == ["grant", "withdraw", "grant"]
    # final fold == granted
    assert (await svc.consent.consent_for(uid, ProcessingPurpose.ANALYTICS)).is_granted


async def test_require_raises_without_consent(session: AsyncSession) -> None:
    svc = _svc(session)
    await svc.bootstrap()
    uid = await _make_user(session)
    with pytest.raises(ConsentRequiredError):
        await svc.consent.require(uid, ProcessingPurpose.MODEL_TRAINING)
    await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.MODEL_TRAINING)
    await svc.consent.require(uid, ProcessingPurpose.MODEL_TRAINING)  # no raise


async def test_new_policy_version_makes_consent_stale(session: AsyncSession) -> None:
    svc = _svc(session)
    await svc.bootstrap()
    uid = await _make_user(session)
    granted = await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.ANALYTICS)
    assert not granted.is_stale

    # publish + activate v2 of the analytics policy
    v2 = await svc.consent.publish(
        PolicyDraft(
            purpose=ProcessingPurpose.ANALYTICS, title="Analytics v2", body="updated terms"
        )
    )
    await svc.consent.activate(v2.id)

    consent = await svc.consent.consent_for(uid, ProcessingPurpose.ANALYTICS)
    assert consent.is_granted and consent.is_stale  # still granted, but to v1


async def test_consent_survives_subject_deletion(session: AsyncSession) -> None:
    """The proof trail (SET NULL) outlives the account (GDPR Art. 7(1))."""
    svc = _svc(session)
    await svc.bootstrap()
    uid = await _make_user(session)
    await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.ANALYTICS)

    user = await session.get(User, uid)
    assert user is not None
    await session.delete(user)
    await session.flush()

    from sqlalchemy import select

    records = (await session.execute(select(ConsentRecord))).scalars().all()
    assert len(records) == 1
    assert records[0].subject_id is None  # subject nulled, proof preserved


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


async def test_ledger_chains_and_verifies(session: AsyncSession) -> None:
    svc = _svc(session)
    await svc.bootstrap()
    uid = await _make_user(session)
    await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.ANALYTICS)
    await svc.consent.withdraw(subject_id=uid, purpose=ProcessingPurpose.ANALYTICS)

    result = await svc.ledger.verify()
    assert result.ok
    assert result.entries >= 2
    assert result.broken_at is None


async def test_ledger_detects_tamper(session: AsyncSession) -> None:
    svc = _svc(session)
    uid = await _make_user(session)
    await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.ANALYTICS)
    await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.PERSONALIZATION)

    # Tamper with the first entry's payload after the fact.
    from sqlalchemy import select

    from app.compliance.db.models import ComplianceLedgerEntry

    first = (
        await session.execute(
            select(ComplianceLedgerEntry).order_by(ComplianceLedgerEntry.seq.asc()).limit(1)
        )
    ).scalar_one()
    first.payload = {"tampered": True}
    await session.flush()

    result = await svc.ledger.verify()
    assert not result.ok
    assert result.broken_at == 1


async def test_ledger_subject_slice(session: AsyncSession) -> None:
    svc = _svc(session)
    a = await _make_user(session, "a@example.com")
    b = await _make_user(session, "b@example.com")
    await svc.consent.grant(subject_id=a, purpose=ProcessingPurpose.ANALYTICS)
    await svc.consent.grant(subject_id=b, purpose=ProcessingPurpose.ANALYTICS)
    await svc.consent.grant(subject_id=a, purpose=ProcessingPurpose.PERSONALIZATION)

    a_slice = await svc.ledger.for_subject(a)
    assert len(a_slice) == 2
    assert all(e.subject_id == a for e in a_slice)
    consent_entries = await svc.ledger.by_category(LedgerCategory.CONSENT)
    assert len(consent_entries) == 3


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


async def test_retention_ttl_expiry(session: AsyncSession) -> None:
    clock = FixedClock(datetime(2026, 6, 1, tzinfo=UTC))
    svc = _svc(session, clock=clock)
    await svc.bootstrap()
    uid = await _make_user(session)

    # READING_SESSION ttl=90d. One old (expired), one fresh (kept).
    old = RetentionItem(
        data_class=DataClass.READING_SESSION,
        subject_id=uid,
        reference_at=datetime(2026, 1, 1, tzinfo=UTC),  # >90d ago
        ref="sess_old",
    )
    fresh = RetentionItem(
        data_class=DataClass.READING_SESSION,
        subject_id=uid,
        reference_at=datetime(2026, 5, 25, tzinfo=UTC),  # <90d ago
        ref="sess_fresh",
    )
    decision = await svc.retention.evaluate([old, fresh])
    expired_refs = {c.item.ref for c in decision.to_expire}
    assert expired_refs == {"sess_old"}


async def test_retention_indefinite_class_never_expires(session: AsyncSession) -> None:
    clock = FixedClock(datetime(2030, 1, 1, tzinfo=UTC))
    svc = _svc(session, clock=clock)
    await svc.bootstrap()
    uid = await _make_user(session)
    item = RetentionItem(
        data_class=DataClass.ACCOUNT,
        subject_id=uid,
        reference_at=datetime(2020, 1, 1, tzinfo=UTC),
        ref="acct",
    )
    decision = await svc.retention.evaluate([item])
    assert decision.expired == 0


async def test_retention_consent_withdrawal_forces_expiry(session: AsyncSession) -> None:
    svc = _svc(session)
    await svc.bootstrap()
    uid = await _make_user(session)
    # grant then withdraw adaptation → uploaded_book (consent-only) becomes due now
    await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.ADAPTATION)
    await svc.consent.withdraw(subject_id=uid, purpose=ProcessingPurpose.ADAPTATION)

    item = RetentionItem(
        data_class=DataClass.UPLOADED_BOOK,
        subject_id=uid,
        reference_at=datetime(2026, 1, 1, tzinfo=UTC),
        ref="book_pdf",
    )
    decision = await svc.retention.evaluate([item])
    assert decision.expired == 1
    assert decision.to_expire[0].reason == "consent withdrawn"


async def test_retention_legal_hold_suspends_expiry(session: AsyncSession) -> None:
    clock = FixedClock(datetime(2026, 6, 1, tzinfo=UTC))
    svc = _svc(session, clock=clock)
    await svc.bootstrap()
    uid = await _make_user(session)
    await svc.holds.place(subject_id=uid, matter_id="LIT-1", reason="litigation")

    old = RetentionItem(
        data_class=DataClass.READING_SESSION,
        subject_id=uid,
        reference_at=datetime(2026, 1, 1, tzinfo=UTC),
        ref="sess_old",
    )
    decision = await svc.retention.evaluate([old])
    assert decision.expired == 0
    assert decision.held == 1
    assert decision.candidates[0].reason == "suspended by legal hold"


async def test_retention_sweep_writes_ledger(session: AsyncSession) -> None:
    svc = _svc(session)
    await svc.bootstrap()
    await svc.retention.sweep([])
    entries = await svc.ledger.by_category(LedgerCategory.RETENTION)
    assert any(e.event == "retention.sweep" for e in entries)


# --------------------------------------------------------------------------- #
# Legal hold
# --------------------------------------------------------------------------- #


async def test_hold_place_and_lift(session: AsyncSession) -> None:
    svc = _svc(session)
    uid = await _make_user(session)
    hold = await svc.holds.place(subject_id=uid, matter_id="M-1", reason="probe")
    assert await svc.holds.is_held(uid)
    await svc.holds.lift(hold.id)
    assert not await svc.holds.is_held(uid)


async def test_hold_double_lift_conflicts(session: AsyncSession) -> None:
    svc = _svc(session)
    uid = await _make_user(session)
    hold = await svc.holds.place(subject_id=uid, matter_id="M-1", reason="probe")
    await svc.holds.lift(hold.id)
    with pytest.raises(ConflictError):
        await svc.holds.lift(hold.id)


async def test_hold_class_scope(session: AsyncSession) -> None:
    svc = _svc(session)
    uid = await _make_user(session)
    await svc.holds.place(
        subject_id=uid, matter_id="M-2", reason="x", data_class=DataClass.GENERATED_MEDIA
    )
    assert await svc.holds.is_held(uid, DataClass.GENERATED_MEDIA)
    assert not await svc.holds.is_held(uid, DataClass.READING_SESSION)


# --------------------------------------------------------------------------- #
# DSAR lifecycle
# --------------------------------------------------------------------------- #


async def test_dsar_happy_path_to_completion(session: AsyncSession) -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))

    class _Fulfiller:
        async def fulfil(self, request) -> FulfilmentResult:  # type: ignore[no-untyped-def]
            return FulfilmentResult(summary={"rows": 7}, artifact_ref="oss://export.zip")

    svc = _svc(session, clock=clock, fulfiller=_Fulfiller())
    uid = await _make_user(session)
    req = await svc.dsar.open_request(subject_id=uid, kind=DSARKind.ACCESS)
    # deadline is one month out.
    assert req.due_at == datetime(2026, 1, 31, tzinfo=UTC)

    await svc.dsar.start_verification(req.id)
    await svc.dsar.begin(req.id)
    completed = await svc.dsar.fulfil(req.id)
    assert completed.state == DSARState.COMPLETED
    assert completed.result is not None
    assert completed.result["artifact_ref"] == "oss://export.zip"

    events = await svc.dsar._repo.events(req.id)  # noqa: SLF001
    assert [e.to_state for e in events] == [
        DSARState.RECEIVED,
        DSARState.VERIFYING,
        DSARState.IN_PROGRESS,
        DSARState.COMPLETED,
    ]


async def test_dsar_illegal_transition_rejected(session: AsyncSession) -> None:
    svc = _svc(session)
    uid = await _make_user(session)
    req = await svc.dsar.open_request(subject_id=uid, kind=DSARKind.ACCESS)
    with pytest.raises(InvalidTransitionError):
        await svc.dsar.begin(req.id)  # RECEIVED → IN_PROGRESS skips verifying


async def test_dsar_extension_extends_deadline_once(session: AsyncSession) -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    svc = _svc(session, clock=clock)
    uid = await _make_user(session)
    req = await svc.dsar.open_request(subject_id=uid, kind=DSARKind.ACCESS)
    await svc.dsar.start_verification(req.id)
    await svc.dsar.begin(req.id)
    extended = await svc.dsar.extend(req.id, reason="complex")
    assert extended.extended_due_at == req.due_at + timedelta(days=60)
    with pytest.raises(ConflictError):
        await svc.dsar.extend(req.id)  # only once


async def test_dsar_erasure_blocked_by_hold(session: AsyncSession) -> None:
    svc = _svc(session)
    uid = await _make_user(session)
    await svc.holds.place(subject_id=uid, matter_id="LIT-9", reason="hold")
    req = await svc.dsar.open_request(subject_id=uid, kind=DSARKind.ERASURE)
    await svc.dsar.start_verification(req.id)
    await svc.dsar.begin(req.id)
    with pytest.raises(LegalHoldError):
        await svc.dsar.fulfil(req.id)


async def test_dsar_cancel_from_open(session: AsyncSession) -> None:
    svc = _svc(session)
    uid = await _make_user(session)
    req = await svc.dsar.open_request(subject_id=uid, kind=DSARKind.PORTABILITY)
    await svc.dsar.start_verification(req.id)
    cancelled = await svc.dsar.cancel(req.id)
    assert cancelled.state == DSARState.CANCELLED


async def test_dsar_overdue_watchlist(session: AsyncSession) -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    svc = _svc(session, clock=clock)
    uid = await _make_user(session)
    req = await svc.dsar.open_request(subject_id=uid, kind=DSARKind.ACCESS)
    await svc.dsar.start_verification(req.id)
    # advance past the deadline
    clock.set(datetime(2026, 3, 1, tzinfo=UTC))
    overdue = await svc.dsar.overdue()
    assert any(v.id == req.id and v.overdue for v in overdue)


async def test_dsar_missing_request_raises(session: AsyncSession) -> None:
    svc = _svc(session)
    with pytest.raises(NotFoundError):
        await svc.dsar.start_verification("does-not-exist")


# --------------------------------------------------------------------------- #
# Cross-cutting report
# --------------------------------------------------------------------------- #


async def test_report_reflects_live_state(session: AsyncSession) -> None:
    svc = _svc(session)
    await svc.bootstrap()
    uid = await _make_user(session)
    # adaptation is required but not granted → DENY
    report = await svc.report(uid)
    assert not report.is_compliant

    await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.ADAPTATION)
    await svc.consent.grant(subject_id=uid, purpose=ProcessingPurpose.MODEL_TRAINING)
    report2 = await svc.report(uid)
    assert report2.is_compliant
