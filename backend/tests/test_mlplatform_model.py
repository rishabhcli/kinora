"""Unit tests for the model value objects (no infra)."""

from __future__ import annotations

import pytest

from app.llmops.errors import InvalidVersionError
from app.mlplatform.serving.errors import RegistryError
from app.mlplatform.serving.model import (
    PROMOTION_LADDER,
    ModelKind,
    ModelProfile,
    ModelVersion,
    Stage,
    next_stage,
    prev_stage,
)


def _profile(**kw: float) -> ModelProfile:
    base: dict[str, float] = {
        "decode_ms_per_token": 5.0,
        "prefill_ms_per_token": 0.5,
        "kv_bytes_per_token": 2048,
        "params_billions": 7.0,
        "cost_per_1k_tokens": 0.002,
    }
    base.update(kw)
    return ModelProfile(**base)  # type: ignore[arg-type]


def test_profile_rejects_bad_values() -> None:
    for bad in (
        {"decode_ms_per_token": 0.0},
        {"prefill_ms_per_token": -1.0},
        {"kv_bytes_per_token": 0},
        {"params_billions": 0.0},
        {"cost_per_1k_tokens": -0.1},
        {"accept_rate": 1.5},
        {"max_context_tokens": 0},
    ):
        with pytest.raises(RegistryError):
            _profile(**bad)


def test_ladder_navigation() -> None:
    assert PROMOTION_LADDER == (Stage.DEV, Stage.STAGING, Stage.CANARY, Stage.PROD)
    assert next_stage(Stage.DEV) == Stage.STAGING
    assert next_stage(Stage.PROD) is None
    assert next_stage(Stage.ARCHIVED) is None
    assert prev_stage(Stage.STAGING) == Stage.DEV
    assert prev_stage(Stage.DEV) is None
    assert prev_stage(Stage.ARCHIVED) is None


def test_model_version_ref_and_semver() -> None:
    mv = ModelVersion("brain", "2.1.0", ModelKind.REASONING, _profile())
    assert mv.ref == "brain@2.1.0"
    assert (mv.semver.major, mv.semver.minor, mv.semver.patch) == (2, 1, 0)
    assert mv.stage == Stage.DEV
    assert mv.gate_passed is False


def test_model_version_rejects_bad_version_and_name() -> None:
    with pytest.raises(InvalidVersionError):
        ModelVersion("brain", "v2", ModelKind.REASONING, _profile())
    with pytest.raises(RegistryError):
        ModelVersion("", "1.0.0", ModelKind.REASONING, _profile())


def test_with_stage_and_with_gate_are_immutable_copies() -> None:
    mv = ModelVersion("brain", "1.0.0", ModelKind.JUDGE, _profile())
    promoted = mv.with_stage(Stage.STAGING)
    gated = mv.with_gate(passed=True)
    assert mv.stage == Stage.DEV  # original unchanged
    assert promoted.stage == Stage.STAGING
    assert gated.gate_passed is True
    assert mv.gate_passed is False
