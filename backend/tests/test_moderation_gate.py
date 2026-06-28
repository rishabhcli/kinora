"""Integration tests for the SafetyGate + ModerationService (isolated DB)."""

from __future__ import annotations

import pytest

from app.db.base import new_id
from app.db.models.user import User
from app.moderation.classifier import KeywordClassifier
from app.moderation.contracts import (
    ContentLabel,
    Decision,
    ModerationContext,
)
from app.moderation.service import ModerationService
from app.moderation.taxonomy import Disposition, ModerationCategory
from app.moderation.tenant_policy import builtin_policies
from tests.moderation_db import mod_session  # noqa: F401  (pytest fixture)

pytestmark = pytest.mark.asyncio


def _service(session, classifier=None) -> ModerationService:
    return ModerationService(session, classifier=classifier or KeywordClassifier())


async def _user(session, *, email: str) -> str:
    """Seed a real user row so a context.user_id satisfies the FK."""
    uid = new_id()
    session.add(User(id=uid, email=email, hashed_password="x"))
    await session.flush()
    return uid


async def test_clean_book_text_passes(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    ctx = ModerationContext(tenant_id="t1", book_id=None, user_id=None)
    res = await svc.screen_book_text("a quiet morning by the lake", context=ctx)
    assert res.decision is Decision.PASS
    assert res.verdict.decision is Disposition.ALLOW
    assert res.event_id is not None  # every screening is recorded
    assert res.review_item_id is None


async def test_disallowed_book_text_rejected_and_queued(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    ctx = ModerationContext(tenant_id="t1")
    res = await svc.screen_book_text("contains csam material", context=ctx)
    assert res.decision is Decision.REJECT
    assert res.verdict.decision is Disposition.BLOCK
    assert res.review_item_id is not None
    # The block produced a queued review item.
    queue = await svc.queue("t1")
    assert any(item.id == res.review_item_id for item in queue)


async def test_clip_gate_blocks_gore_frame(mod_session) -> None:  # noqa: F811
    frame = b"frame-gore"
    clf = KeywordClassifier(frame_labels={frame: [ContentLabel.of(ModerationCategory.GORE, 0.9)]})
    svc = _service(mod_session, clf)
    uid = await _user(mod_session, email="gore@example.com")
    ctx = ModerationContext(tenant_id="t1", shot_id="shot_1", user_id=uid)
    res = await svc.screen_clip([frame], context=ctx)
    assert res.decision is Decision.REJECT
    assert ModerationCategory.GORE in res.verdict.categories


async def test_clip_gate_merges_narration(mod_session) -> None:  # noqa: F811
    # A clean frame but hateful narration → the merged verdict blocks.
    frame = b"clean-frame"
    svc = _service(mod_session)
    ctx = ModerationContext(tenant_id="t1", shot_id="shot_2")
    res = await svc.screen_clip([frame], context=ctx, narration="this is hate speech")
    assert res.decision is Decision.REJECT
    assert ModerationCategory.HATE in res.verdict.categories


async def test_flagged_content_served_under_default(mod_session) -> None:  # noqa: F811
    # A MEDIUM sexual flag PASSes (served) under the default policy but is queued.
    frame = b"frame-sexual"
    clf = KeywordClassifier(
        frame_labels={frame: [ContentLabel.of(ModerationCategory.SEXUAL, 0.5)]}
    )
    svc = _service(mod_session, clf)
    ctx = ModerationContext(tenant_id="t1")
    res = await svc.screen_keyframe(frame, context=ctx)
    assert res.verdict.decision is Disposition.FLAG
    assert res.decision is Decision.PASS  # default serve_flagged=True
    assert res.review_item_id is not None  # but surfaced for review


async def test_children_policy_holds_flagged(mod_session) -> None:  # noqa: F811
    # The children tenant has serve_flagged=False, so a flag is HELD not served.
    await _service(mod_session).set_policy(builtin_policies()["children"])
    frame = b"frame-mild-violence"
    clf = KeywordClassifier(
        frame_labels={frame: [ContentLabel.of(ModerationCategory.VIOLENCE, 0.3)]}
    )
    svc = _service(mod_session, clf)
    ctx = ModerationContext(tenant_id="children")
    res = await svc.screen_keyframe(frame, context=ctx)
    # Children policy escalates violence; 0.3*1.4 ~ LOW→MEDIUM bucket → block.
    assert res.decision in {Decision.HOLD, Decision.REJECT}


async def test_persisted_policy_round_trips(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    mature = builtin_policies()["mature"]
    await svc.set_policy(mature)
    resolved = await svc.resolve_policy("mature")
    assert resolved.version == mature.version
    assert resolved.strictness == pytest.approx(mature.strictness)


async def test_every_decision_is_recorded(mod_session) -> None:  # noqa: F811
    svc = _service(mod_session)
    ctx = ModerationContext(tenant_id="t9")
    await svc.screen_book_text("a clean line", context=ctx)
    await svc.screen_book_text("contains csam", context=ctx)
    stats = await svc.event_stats("t9")
    assert stats["decisions"]["allow"] == 1
    assert stats["decisions"]["block"] == 1


async def test_degraded_ingest_fails_closed(mod_session) -> None:  # noqa: F811
    class DegradedClassifier(KeywordClassifier):
        async def classify_text(self, text, *, surface):  # type: ignore[override]
            from app.moderation.contracts import ClassificationResult

            return ClassificationResult(
                surface=surface,
                labels=[ContentLabel.of(ModerationCategory.SAFE, 0.0)],
                classifier="degraded",
                degraded=True,
            )

    svc = _service(mod_session, DegradedClassifier())
    ctx = ModerationContext(tenant_id="t1")
    res = await svc.screen_book_text("we could not classify this", context=ctx)
    # Ingest fails closed: a content we could not screen is held for review.
    assert res.decision is Decision.HOLD
    assert res.review_item_id is not None


async def test_degraded_generation_fails_open(mod_session) -> None:  # noqa: F811
    from app.moderation.contracts import ClassificationResult

    class DegradedVision(KeywordClassifier):
        async def classify_frames(self, frames, *, surface):  # type: ignore[override]
            return ClassificationResult(
                surface=surface,
                labels=[ContentLabel.of(ModerationCategory.SAFE, 0.0)],
                classifier="degraded",
                degraded=True,
            )

    svc = _service(mod_session, DegradedVision())
    ctx = ModerationContext(tenant_id="t1")
    res = await svc.screen_keyframe(b"x", context=ctx)
    # Generation fails open: a transient blip never silently drops a clip.
    assert res.decision is Decision.PASS
