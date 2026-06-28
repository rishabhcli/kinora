"""Integration tests for the review workflow + escalation + audit (isolated DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.base import new_id
from app.db.models.user import User
from app.moderation.audit import ModerationAuditLog
from app.moderation.classifier import KeywordClassifier
from app.moderation.contracts import (
    ContentLabel,
    ModerationContext,
    ReviewState,
)
from app.moderation.escalation import (
    EnforcementTier,
    EscalationPolicy,
    EscalationService,
)
from app.moderation.repositories import (
    ModerationAuditRepo,
    ViolationCounterRepo,
)
from app.moderation.review import ReviewTransitionError
from app.moderation.service import ModerationService
from app.moderation.taxonomy import ModerationCategory, Severity
from tests.moderation_db import mod_session  # noqa: F401  (pytest fixture)

pytestmark = pytest.mark.asyncio


def _service(session) -> ModerationService:
    return ModerationService(session, classifier=KeywordClassifier())


async def _user(session, *, email: str) -> str:
    uid = new_id()
    session.add(User(id=uid, email=email, hashed_password="x"))
    await session.flush()
    return uid


# --------------------------------------------------------------------------- #
# Review workflow
# --------------------------------------------------------------------------- #


async def test_review_happy_path_claim_approve(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    ctx = ModerationContext(tenant_id="t1")
    res = await svc.screen_book_text("contains csam", context=ctx)
    item_id = res.review_item_id
    assert item_id is not None

    await svc.review.claim(item_id, reviewer_id="rev1")
    item = await svc.review.view(item_id)
    assert item is not None and item.state is ReviewState.UNDER_REVIEW

    await svc.review.approve(item_id, reviewer_id="rev1", note="false positive")
    item = await svc.review.view(item_id)
    assert item is not None and item.state is ReviewState.APPROVED


async def test_review_reject_then_appeal_then_grant(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    res = await svc.screen_book_text("contains csam", context=ModerationContext(tenant_id="t1"))
    item_id = res.review_item_id
    assert item_id is not None
    await svc.review.claim(item_id, reviewer_id="rev1")
    await svc.review.reject(item_id, reviewer_id="rev1")
    await svc.review.appeal(item_id, appellant_id="owner")
    await svc.review.grant_appeal(item_id, reviewer_id="senior")
    item = await svc.review.view(item_id)
    assert item is not None and item.state is ReviewState.APPEAL_GRANTED


async def test_review_rejects_illegal_transition(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    res = await svc.screen_book_text("contains csam", context=ModerationContext(tenant_id="t1"))
    item_id = res.review_item_id
    assert item_id is not None
    # Cannot approve a PENDING item without claiming it first.
    with pytest.raises(ReviewTransitionError):
        await svc.review.approve(item_id, reviewer_id="rev1")


async def test_review_history_is_appended(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    res = await svc.screen_book_text("contains csam", context=ModerationContext(tenant_id="t1"))
    item_id = res.review_item_id
    assert item_id is not None
    await svc.review.claim(item_id, reviewer_id="rev1")
    await svc.review.reject(item_id, reviewer_id="rev1", note="upheld")
    row = await svc.review._repo.get(item_id)  # type: ignore[attr-defined]
    states = [h["state"] for h in (row.state_history or [])]
    assert states == ["pending", "under_review", "rejected"]
    assert row.resolver_id == "rev1"
    assert row.resolved_at is not None


async def test_queue_filters_by_state(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    ctx = ModerationContext(tenant_id="tq")
    await svc.screen_book_text("contains csam", context=ctx)  # block → pending
    await svc.screen_book_text("build a bomb now", context=ctx)  # block → pending
    pending = await svc.queue("tq", state=ReviewState.PENDING)
    assert len(pending) == 2
    approved = await svc.queue("tq", state=ReviewState.APPROVED)
    assert approved == []


# --------------------------------------------------------------------------- #
# Escalation persistence (rate-of-violation + repeat-offender ladder)
# --------------------------------------------------------------------------- #


async def test_repeat_offender_escalates(mod_session) -> None:  # noqa: F811
    audit = ModerationAuditLog(ModerationAuditRepo(mod_session))
    policy = EscalationPolicy(
        warn_at=1, throttle_at=2, suspend_at=3, ban_at=4, severity_weight=1
    )
    svc = EscalationService(ViolationCounterRepo(mod_session), audit, policy=policy)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tiers = []
    for i in range(4):
        out = await svc.record_violation(
            tenant_id="t1",
            actor_id="badactor",
            severity=Severity.MEDIUM,
            categories=[ModerationCategory.HATE],
            now=now + timedelta(minutes=i),
        )
        tiers.append(out.tier)
    assert tiers == [
        EnforcementTier.WARNED,
        EnforcementTier.THROTTLED,
        EnforcementTier.SUSPENDED,
        EnforcementTier.BANNED,
    ]
    # The banned actor is generation-blocked.
    status = await svc.status(tenant_id="t1", actor_id="badactor", now=now + timedelta(minutes=5))
    assert status.tier is EnforcementTier.BANNED
    assert status.generation_blocked is True


async def test_window_decay_resets_tier(mod_session) -> None:  # noqa: F811
    audit = ModerationAuditLog(ModerationAuditRepo(mod_session))
    policy = EscalationPolicy(
        warn_at=1, throttle_at=2, suspend_at=3, ban_at=8, window=timedelta(hours=1)
    )
    svc = EscalationService(ViolationCounterRepo(mod_session), audit, policy=policy)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    await svc.record_violation(
        tenant_id="t1", actor_id="a", severity=Severity.MEDIUM, now=start
    )
    # After the window elapses, the standing decays back to clean.
    later = start + timedelta(hours=2)
    status = await svc.status(tenant_id="t1", actor_id="a", now=later)
    assert status.tier is EnforcementTier.CLEAN
    # A fresh violation after the window starts a new window at WARNED.
    out = await svc.record_violation(
        tenant_id="t1", actor_id="a", severity=Severity.MEDIUM, now=later
    )
    assert out.tier is EnforcementTier.WARNED


async def test_reinstate_clears_ban(mod_session) -> None:  # noqa: F811
    audit = ModerationAuditLog(ModerationAuditRepo(mod_session))
    policy = EscalationPolicy(warn_at=1, throttle_at=1, suspend_at=1, ban_at=1)
    svc = EscalationService(ViolationCounterRepo(mod_session), audit, policy=policy)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    out = await svc.record_violation(
        tenant_id="t1", actor_id="a", severity=Severity.CRITICAL, now=now
    )
    assert out.tier is EnforcementTier.BANNED
    reinstated = await svc.reinstate(tenant_id="t1", actor_id="a", reviewer_id="admin", now=now)
    assert reinstated.tier is EnforcementTier.CLEAN
    assert reinstated.generation_blocked is False


async def test_gate_block_advances_offender_tally(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    uid = await _user(mod_session, email="repeat@example.com")
    ctx = ModerationContext(tenant_id="t1", user_id=uid)
    await svc.screen_book_text("contains csam", context=ctx)
    status = await svc.actor_status(tenant_id="t1", actor_id=uid)
    assert status.total_count == 1
    assert status.tier >= EnforcementTier.WARNED


async def test_review_reject_advances_offender_tally(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    uid = await _user(mod_session, email="rev@example.com")
    # A FLAG (not a hard block) does not auto-count at the gate, but a human reject does.
    frame = b"flag-frame"
    flag_clf = KeywordClassifier(
        frame_labels={frame: [ContentLabel.of(ModerationCategory.SEXUAL, 0.5)]}
    )
    svc2 = ModerationService(mod_session, classifier=flag_clf)
    ctx = ModerationContext(tenant_id="t1", user_id=uid)
    res = await svc2.screen_keyframe(frame, context=ctx)
    assert res.review_item_id is not None
    # Gate flag did not count yet.
    before = await svc.actor_status(tenant_id="t1", actor_id=uid)
    assert before.total_count == 0
    # Reviewer upholds → counts against the actor.
    await svc2.review.claim(res.review_item_id, reviewer_id="rev1")
    await svc2.review.reject(res.review_item_id, reviewer_id="rev1")
    after = await svc.actor_status(tenant_id="t1", actor_id=uid)
    assert after.total_count == 1


# --------------------------------------------------------------------------- #
# Audit chain (tamper-evident)
# --------------------------------------------------------------------------- #


async def test_audit_chain_is_intact_after_screening(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    ctx = ModerationContext(tenant_id="taudit")
    await svc.screen_book_text("a clean line", context=ctx)
    await svc.screen_book_text("contains csam", context=ctx)
    chain = await svc.audit_chain("taudit")
    assert chain.intact is True
    assert chain.broken_at_seq is None
    assert len(chain.entries) >= 2
    # Sequence is monotone from 1.
    seqs = [e.seq for e in chain.entries]
    assert seqs == sorted(seqs)
    assert seqs[0] == 1


async def test_audit_chain_detects_tampering(mod_session) -> None:  # noqa: F811
    repo = ModerationAuditRepo(mod_session)
    log = ModerationAuditLog(repo)
    from app.moderation.audit import AuditAction

    await log.record(tenant_id="ttamper", action=AuditAction.SCREEN, actor_id="s", payload={"a": 1})
    await log.record(tenant_id="ttamper", action=AuditAction.SCREEN, actor_id="s", payload={"a": 2})
    await log.record(tenant_id="ttamper", action=AuditAction.SCREEN, actor_id="s", payload={"a": 3})
    chain = await log.replay("ttamper")
    assert chain.intact is True

    # Tamper with the middle row's payload directly (retroactive edit).
    rows = await repo.replay("ttamper")
    target = rows[1]
    target.payload = {"a": 999}
    await mod_session.flush()

    chain = await log.replay("ttamper")
    assert chain.intact is False
    assert chain.broken_at_seq == 2


async def test_audit_chains_are_per_tenant(mod_session) -> None:  # noqa: F811
    log = ModerationAuditLog(ModerationAuditRepo(mod_session))
    from app.moderation.audit import AuditAction

    await log.record(tenant_id="A", action=AuditAction.SCREEN, actor_id="s")
    await log.record(tenant_id="B", action=AuditAction.SCREEN, actor_id="s")
    await log.record(tenant_id="A", action=AuditAction.SCREEN, actor_id="s")
    a = await log.replay("A")
    b = await log.replay("B")
    assert [e.seq for e in a.entries] == [1, 2]
    assert [e.seq for e in b.entries] == [1]
    assert a.intact and b.intact
