"""Unit tests for the immutable, hash-chained decision log + appeal/override hooks."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from app.safety.contracts import (
    AppealState,
    Finding,
    OutputAssessment,
    OutputVerdict,
    PromptDecision,
    SafetyAction,
    SafetyCategory,
    SafetyContext,
    SafetySurface,
    Severity,
)
from app.safety.decision_log import GENESIS_HASH, InMemoryDecisionLog

pytestmark = pytest.mark.asyncio

_FIXED = datetime(2026, 1, 1, tzinfo=UTC)


def _prompt_decision(action: SafetyAction = SafetyAction.BLOCK) -> PromptDecision:
    return PromptDecision(
        surface=SafetySurface.PROMPT,
        action=action,
        severity=Severity.HIGH,
        driving_findings=[Finding.of(SafetyCategory.GORE, 0.7)],
        effective_prompt="some prompt",
        reason="blocked (gore)",
    )


def _output_assessment() -> OutputAssessment:
    return OutputAssessment(
        surface=SafetySurface.CLIP,
        verdict=OutputVerdict.QUARANTINE,
        severity=Severity.MEDIUM,
        driving_findings=[Finding.of(SafetyCategory.SEXUAL, 0.5)],
        sampled_frames=3,
        reason="held",
    )


async def test_records_form_a_chain() -> None:
    log = InMemoryDecisionLog(now=_FIXED)
    ctx = SafetyContext(tenant_id="t1", shot_id="s1")
    r1 = await log.record_prompt(_prompt_decision(), context=ctx)
    r2 = await log.record_output(_output_assessment(), context=ctx)
    assert r1.seq == 1
    assert r1.prev_hash == GENESIS_HASH
    assert r2.seq == 2
    assert r2.prev_hash == r1.this_hash  # linked


async def test_chain_verifies_intact() -> None:
    log = InMemoryDecisionLog(now=_FIXED)
    ctx = SafetyContext(tenant_id="t1")
    for _ in range(5):
        await log.record_prompt(_prompt_decision(), context=ctx)
    verification = await log.verify("t1")
    assert verification.intact
    assert verification.length == 5
    assert verification.first_broken_seq is None


async def test_tamper_detected() -> None:
    log = InMemoryDecisionLog(now=_FIXED)
    ctx = SafetyContext(tenant_id="t1")
    await log.record_prompt(_prompt_decision(), context=ctx)
    await log.record_prompt(_prompt_decision(SafetyAction.ALLOW), context=ctx)
    await log.record_prompt(_prompt_decision(), context=ctx)
    # Tamper with record #2's payload in place (the chain is private; mutate it).
    chain = log._chains["t1"]  # noqa: SLF001 - white-box tamper test
    tampered = dataclasses.replace(chain[1], action="allow_but_edited")
    chain[1] = tampered
    verification = await log.verify("t1")
    assert not verification.intact
    assert verification.first_broken_seq == 2


async def test_per_tenant_chains_independent() -> None:
    log = InMemoryDecisionLog(now=_FIXED)
    await log.record_prompt(_prompt_decision(), context=SafetyContext(tenant_id="a"))
    await log.record_prompt(_prompt_decision(), context=SafetyContext(tenant_id="b"))
    assert len(await log.history("a")) == 1
    assert len(await log.history("b")) == 1
    assert (await log.verify("a")).intact
    assert (await log.verify("b")).intact


async def test_override_is_appended_not_mutated() -> None:
    log = InMemoryDecisionLog(now=_FIXED)
    ctx = SafetyContext(tenant_id="t1")
    original = await log.record_prompt(_prompt_decision(), context=ctx)
    override = await log.record_override(
        record_id=original.id,
        context=ctx,
        new_action="allow",
        actor_id="ops-1",
        reason="false positive",
    )
    # Original is untouched; override references it.
    assert override.references == original.id
    assert override.action == "allow"
    history = await log.history("t1")
    assert len(history) == 2
    assert history[0].action == str(SafetyAction.BLOCK)  # original unchanged
    assert (await log.verify("t1")).intact


async def test_appeal_lifecycle() -> None:
    log = InMemoryDecisionLog(now=_FIXED)
    ctx = SafetyContext(tenant_id="t1")
    original = await log.record_prompt(_prompt_decision(), context=ctx)
    await log.request_appeal(record_id=original.id, context=ctx, reason="please review")
    assert await log.effective_appeal_state(original.id) is AppealState.REQUESTED
    await log.resolve_appeal(
        record_id=original.id,
        context=ctx,
        granted=True,
        actor_id="ops-1",
        reason="reinstated",
    )
    assert await log.effective_appeal_state(original.id) is AppealState.GRANTED
    assert (await log.verify("t1")).intact


async def test_override_unknown_record_raises() -> None:
    log = InMemoryDecisionLog(now=_FIXED)
    with pytest.raises(KeyError):
        await log.record_override(
            record_id="nope",
            context=SafetyContext(tenant_id="t1"),
            new_action="allow",
            actor_id="x",
            reason="y",
        )


async def test_record_to_view_projection() -> None:
    log = InMemoryDecisionLog(now=_FIXED)
    ctx = SafetyContext(tenant_id="t1", shot_id="s9", book_id="b9")
    rec = await log.record_prompt(_prompt_decision(), context=ctx)
    view = rec.to_view()
    assert view.seq == 1
    assert view.shot_id == "s9"
    assert view.surface is SafetySurface.PROMPT
    assert view.this_hash == rec.this_hash
