"""Observability: OTel tracing is a clean no-op unless explicitly configured."""

from __future__ import annotations

import importlib.util

import pytest
from fastapi import FastAPI

from app.observability import tracing
from app.observability.tracing import OTLP_ENDPOINT_ENV, init_tracing, tracing_enabled


def _otel_installed() -> bool:
    """True only when the optional OpenTelemetry stack is importable."""
    try:
        return importlib.util.find_spec("opentelemetry.sdk.trace") is not None
    except ModuleNotFoundError:
        return False


_OTEL_INSTALLED = _otel_installed()


def _reset_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the process-level one-shot guard so each test starts fresh."""
    monkeypatch.setattr(tracing, "_initialized", False, raising=False)


def test_tracing_is_noop_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OTLP_ENDPOINT_ENV, raising=False)
    _reset_init(monkeypatch)
    app = FastAPI()

    assert tracing_enabled() is False
    # No env var → a clean no-op that never raises and never instruments.
    assert init_tracing(app) is False


def test_tracing_enabled_flag_follows_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT_ENV, "http://collector.invalid:4318")
    assert tracing_enabled() is True
    monkeypatch.delenv(OTLP_ENDPOINT_ENV, raising=False)
    assert tracing_enabled() is False


def test_init_tracing_when_configured_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT_ENV, "http://collector.invalid:4318")
    _reset_init(monkeypatch)
    app = FastAPI()

    # Wires when the optional deps are present; otherwise it is a graceful no-op
    # (logs and returns False) — never an import error at startup.
    result = init_tracing(app, service_name="kinora-test")
    assert isinstance(result, bool)
    if _OTEL_INSTALLED:
        assert result is True
    else:
        assert result is False
