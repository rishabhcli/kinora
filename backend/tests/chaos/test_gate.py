"""Tests for the production hard gate — chaos refuses to arm outside local/test."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.chaos.gate import (
    CHAOS_SAFE_ENVIRONMENTS,
    ChaosDisarmedError,
    assert_chaos_armable,
    evaluate_gate,
)


@dataclass
class _Settings:
    app_env: str
    chaos_enabled: bool


def test_gate_allows_local_with_flag() -> None:
    decision = evaluate_gate(_Settings(app_env="local", chaos_enabled=True))
    assert decision.allowed
    assert_chaos_armable(_Settings(app_env="local", chaos_enabled=True))


def test_gate_refuses_local_without_flag() -> None:
    decision = evaluate_gate(_Settings(app_env="local", chaos_enabled=False))
    assert not decision.allowed
    assert "off by default" in decision.reason
    with pytest.raises(ChaosDisarmedError):
        assert_chaos_armable(_Settings(app_env="local", chaos_enabled=False))


def test_gate_refuses_prod_even_with_flag_set() -> None:
    # The whole point: an accidental CHAOS_ENABLED=true in prod must NOT arm.
    settings = _Settings(app_env="production", chaos_enabled=True)
    decision = evaluate_gate(settings)
    assert not decision.allowed
    assert "not a chaos-safe environment" in decision.reason
    with pytest.raises(ChaosDisarmedError) as ei:
        assert_chaos_armable(settings)
    assert ei.value.environment == "production"
    assert ei.value.flag_enabled is True


@pytest.mark.parametrize("env", sorted(CHAOS_SAFE_ENVIRONMENTS))
def test_all_safe_environments_allow_with_flag(env: str) -> None:
    assert evaluate_gate(_Settings(app_env=env, chaos_enabled=True)).allowed


@pytest.mark.parametrize("env", ["production", "prod", "staging", "PROD", ""])
def test_unsafe_environments_always_refuse(env: str) -> None:
    assert not evaluate_gate(_Settings(app_env=env, chaos_enabled=True)).allowed


def test_env_case_insensitive() -> None:
    assert evaluate_gate(_Settings(app_env="LOCAL", chaos_enabled=True)).allowed


def test_real_settings_satisfy_gate_protocol_and_refuse_by_default() -> None:
    # The real Settings has chaos_enabled defaulting False; conftest pins
    # APP_ENV=local, so the default config refuses to arm (deny-by-default).
    from app.core.config import get_settings

    settings = get_settings()
    assert hasattr(settings, "chaos_enabled")
    assert settings.chaos_enabled is False
    with pytest.raises(ChaosDisarmedError):
        assert_chaos_armable(settings)
