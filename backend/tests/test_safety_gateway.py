"""Integration tests for the SafetyGateway façade (offline, deterministic)."""

from __future__ import annotations

import pytest

from app.safety import build_default_gateway
from app.safety.classifier import KeywordSafetyClassifier
from app.safety.config import SafetySettings
from app.safety.contracts import (
    AgeRating,
    Finding,
    OutputVerdict,
    SafetyAction,
    SafetyCategory,
    SafetyContext,
)
from app.safety.gateway import SafetyGateway

pytestmark = pytest.mark.asyncio


def _gateway(
    frame_findings: dict[bytes, list[Finding]] | None = None,
) -> SafetyGateway:
    return SafetyGateway(
        classifier=KeywordSafetyClassifier(frame_findings=frame_findings or {})
    )


async def test_default_gateway_is_offline() -> None:
    # build_default_gateway with no providers uses the deterministic keyword fake.
    gw = build_default_gateway()
    decision = await gw.screen_prompt("a calm harbour at dawn")
    assert decision.action is SafetyAction.ALLOW


async def test_prompt_screen_records_decision() -> None:
    gw = _gateway()
    ctx = SafetyContext(tenant_id="t1", shot_id="s1")
    await gw.screen_prompt("a graphic stabbing", context=ctx)
    history = await gw.history("t1")
    assert len(history) == 1
    assert history[0].action == SafetyAction.TRANSFORM.value


async def test_output_screen_records_verdict() -> None:
    bad = b"nsfw"
    gw = _gateway(frame_findings={bad: [Finding.of(SafetyCategory.SEXUAL, 0.5)]})
    ctx = SafetyContext(tenant_id="t1", shot_id="s1")
    assessment = await gw.screen_output([bad], context=ctx)
    assert assessment.verdict is OutputVerdict.QUARANTINE
    history = await gw.history("t1")
    assert history[0].action == OutputVerdict.QUARANTINE.value


async def test_full_loop_chain_stays_intact() -> None:
    gw = _gateway()
    ctx = SafetyContext(tenant_id="t1")
    await gw.screen_prompt("a graphic stabbing", context=ctx)
    await gw.screen_prompt("a peaceful field", context=ctx)
    await gw.screen_output([b"clean"], context=ctx)
    assert await gw.verify_log("t1")


async def test_tag_book_produces_advisory_and_records() -> None:
    gw = _gateway()
    ctx = SafetyContext(tenant_id="t1", book_id="b1")
    advisory = await gw.tag_book(
        "The battle was brutal, with a graphic stabbing and an explicit sex scene.",
        context=ctx,
    )
    assert advisory.rating.rank >= AgeRating.R.rank
    history = await gw.history("t1")
    assert history[0].action == advisory.rating.value


async def test_override_through_gateway() -> None:
    gw = _gateway()
    ctx = SafetyContext(tenant_id="t1")
    await gw.screen_prompt("csam content", context=ctx)
    history = await gw.history("t1")
    original_id = history[0].id
    await gw.override(
        record_id=original_id,
        context=ctx,
        new_action="allow",
        actor_id="ops",
        reason="manual review cleared",
    )
    assert await gw.verify_log("t1")
    assert len(await gw.history("t1")) == 2


async def test_disabled_gateway_passes_through() -> None:
    gw = SafetyGateway(
        classifier=KeywordSafetyClassifier(),
        settings=SafetySettings(enabled=False),
    )
    decision = await gw.screen_prompt("csam content")  # would normally BLOCK
    assert decision.action is SafetyAction.ALLOW
    assert "disabled" in decision.reason


async def test_softening_disabled_quarantines_instead_of_transforms() -> None:
    gw = SafetyGateway(
        classifier=KeywordSafetyClassifier(),
        settings=SafetySettings(enable_softening=False),
    )
    decision = await gw.screen_prompt("a graphic stabbing")
    # With softening off the literary violence cannot be transformed away.
    assert decision.action is not SafetyAction.TRANSFORM


async def test_plan_for_prompt_helper() -> None:
    gw = _gateway()
    plan = await gw.plan_for_prompt("an explicit sex scene")
    # Strict providers refuse explicit content pre-softening.
    assert "dashscope" not in plan.ordered_providers
