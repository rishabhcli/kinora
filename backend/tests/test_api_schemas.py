"""Pure-pydantic validation tests for request schemas (no infra).

The scheduler clamps velocity downstream (``clamp_velocity`` abs+clamps to
[0.5×,3×]), but ``min``/``max`` with NaN is unreliable, so the API boundary must
reject non-finite and out-of-range reading-intent values up front (§4.3).
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.api.schemas import IntentRequest, ShotResponse


def test_intent_request_accepts_normal_values() -> None:
    req = IntentRequest(focus_word=10, velocity=4.0)
    assert req.focus_word == 10
    assert req.velocity == 4.0


def test_intent_request_rejects_negative_focus_word() -> None:
    with pytest.raises(ValidationError):
        IntentRequest(focus_word=-1, velocity=4.0)


def test_intent_request_rejects_negative_velocity() -> None:
    with pytest.raises(ValidationError):
        IntentRequest(focus_word=0, velocity=-1.0)


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
def test_intent_request_rejects_non_finite_velocity(bad: float) -> None:
    with pytest.raises(ValidationError):
        IntentRequest(focus_word=0, velocity=bad)


def test_intent_request_rejects_absurd_velocity() -> None:
    with pytest.raises(ValidationError):
        IntentRequest(focus_word=0, velocity=1e9)


def test_shot_response_exposes_clip_offsets() -> None:
    # ShotResponse has no ConfigDict(extra=...) and no from_attributes/
    # model_validate(ORM) convention anywhere in this codebase; every call site
    # (e.g. app.api.routes.books._shot_response) builds it via explicit
    # field-by-field kwargs, so exercise that same direct-construction path.
    response = ShotResponse(
        shot_id="s1", status="planned", clip_start_s=5.0, clip_end_s=10.0
    )
    assert response.clip_start_s == 5.0
    assert response.clip_end_s == 10.0
