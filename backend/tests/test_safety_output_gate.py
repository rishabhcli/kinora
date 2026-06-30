"""Unit tests for the post-generation output gate (sampled frames → verdict)."""

from __future__ import annotations

import pytest

from app.safety.classifier import KeywordSafetyClassifier
from app.safety.contracts import (
    Finding,
    OutputVerdict,
    PromptAssessment,
    SafetyCategory,
    SafetySurface,
)
from app.safety.output_gate import OutputGate

pytestmark = pytest.mark.asyncio


def _gate(
    frame_findings: dict[bytes, list[Finding]] | None = None,
    *,
    fail_closed: bool = False,
) -> OutputGate:
    return OutputGate(
        classifier=KeywordSafetyClassifier(frame_findings=frame_findings or {}),
        fail_closed=fail_closed,
    )


async def test_clean_frames_allow() -> None:
    assessment = await _gate().screen_frames([b"frame1", b"frame2"])
    assert assessment.verdict is OutputVerdict.ALLOW
    assert assessment.allowed
    assert assessment.sampled_frames == 2


async def test_nsfw_frame_quarantines() -> None:
    nsfw = b"nsfw-frame"
    gate = _gate({nsfw: [Finding.of(SafetyCategory.SEXUAL, 0.5, source="fake")]})
    assessment = await gate.screen_frames([nsfw])
    # MEDIUM sexual ⇒ quarantine_at MEDIUM on output (transform escalated).
    assert assessment.verdict is OutputVerdict.QUARANTINE
    assert SafetyCategory.SEXUAL in assessment.categories


async def test_gore_frame_quarantines() -> None:
    gore = b"gore-frame"
    gate = _gate({gore: [Finding.of(SafetyCategory.GORE, 0.5, source="fake")]})
    assessment = await gate.screen_frames([gore])
    assert assessment.verdict is OutputVerdict.QUARANTINE


async def test_zero_tolerance_frame_blocks() -> None:
    bad = b"bad-frame"
    gate = _gate({bad: [Finding.of(SafetyCategory.SEXUAL_MINORS, 0.3, source="fake")]})
    assessment = await gate.screen_frames([bad])
    assert assessment.verdict is OutputVerdict.BLOCK


async def test_transformable_finding_escalates_to_quarantine_on_output() -> None:
    # VIOLENCE at LOW would TRANSFORM pre-generation; on output there is no prompt to
    # rewrite, so it must QUARANTINE rather than silently pass.
    v = b"violent-frame"
    gate = _gate({v: [Finding.of(SafetyCategory.VIOLENCE, 0.3, source="fake")]})
    assessment = await gate.screen_frames([v])
    assert assessment.verdict is OutputVerdict.QUARANTINE


async def test_empty_frames_allow() -> None:
    assessment = await _gate().screen_frames([])
    assert assessment.verdict is OutputVerdict.ALLOW
    assert assessment.sampled_frames == 0


class _DegradedClassifier:
    """A fake whose frame path always reports a degraded (failed) classification."""

    name = "degraded"

    async def classify_text(
        self, text: str, *, surface: SafetySurface
    ) -> PromptAssessment:  # pragma: no cover - unused here
        return PromptAssessment(surface=surface, findings=[], degraded=True)

    async def classify_frames(
        self, frames: list[bytes], *, surface: SafetySurface
    ) -> PromptAssessment:
        return PromptAssessment(
            surface=surface,
            findings=[Finding.of(SafetyCategory.SAFE, 0.0, source="fake")],
            classifier=self.name,
            degraded=True,
        )


async def test_degraded_classifier_fails_open_by_default() -> None:
    gate = OutputGate(classifier=_DegradedClassifier(), fail_closed=False)
    assessment = await gate.screen_frames([b"f1"])
    assert assessment.verdict is OutputVerdict.ALLOW
    assert assessment.degraded


async def test_degraded_classifier_fails_closed_when_configured() -> None:
    gate = OutputGate(classifier=_DegradedClassifier(), fail_closed=True)
    assessment = await gate.screen_frames([b"f1"])
    assert assessment.verdict is OutputVerdict.QUARANTINE
    assert assessment.degraded
