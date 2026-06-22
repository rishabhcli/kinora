"""Settings boot guards (kinora.md §12) — the JWT-secret production guard (Fix 9)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import DEFAULT_JWT_SECRET, Settings

_REAL_SECRET = "a-real-production-jwt-secret-32+bytes"


def test_local_boots_with_the_default_jwt_secret() -> None:
    settings = Settings(dashscope_api_key="test", app_env="local")
    assert settings.is_local
    assert settings.jwt_secret == DEFAULT_JWT_SECRET


def test_nonlocal_with_default_jwt_secret_refuses_to_boot() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(dashscope_api_key="test", app_env="production")


def test_nonlocal_with_a_real_jwt_secret_boots() -> None:
    settings = Settings(
        dashscope_api_key="test", app_env="production", jwt_secret=_REAL_SECRET
    )
    assert not settings.is_local
    assert settings.jwt_secret == _REAL_SECRET
