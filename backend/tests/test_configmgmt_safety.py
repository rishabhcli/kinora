"""Production-safety gate (app.configmgmt.safety). Pure — no infra/network.

Asserts the boot-refusal invariants: demo creds / insecure placeholders refused
in prod, KINORA_LIVE_VIDEO armed only with an explicit prod opt-in, debug/chaos
posture fatal, validator ERRORs escalated to FATAL, and local always passes. The
gate's environ is injected so no real env var is read or mutated.
"""

from __future__ import annotations

import pytest

from app.configmgmt.errors import ProdSafetyError, Severity
from app.configmgmt.safety import (
    CHAOS_ARMED_ENV,
    PROD_LIVE_VIDEO_OPT_IN_ENV,
    ProdSafetyGate,
    assert_safe_to_boot,
)
from app.core.config import Settings

_REAL = "a-real-production-secret-value-32+bytes"


def _prod(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "dashscope_api_key": "real-key",
        "app_env": "prod",
        "jwt_secret": _REAL,
        "api_key_pepper": _REAL + "-pepper",
        "s3_access_key": "real-access",
        "s3_secret_key": "real-secret",
        "s3_public_base_url": "https://cdn.example.com/kinora",
        "mcp_auth_token": "mcp-tok",
        "billing_webhook_secret": "whsec_real",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _fatal_codes(gate: ProdSafetyGate, s: Settings) -> set[str]:
    return {f.code for f in gate.evaluate(s).fatal}


# --------------------------------------------------------------------------- #
# Local is a near no-op
# --------------------------------------------------------------------------- #


def test_local_is_always_safe() -> None:
    s = Settings(dashscope_api_key="test", app_env="local")  # demo defaults
    report = assert_safe_to_boot(s, environ={})
    assert report.safe
    assert report.fatal == ()


def test_local_with_live_video_armed_is_not_refused() -> None:
    # Locally live-video is a guarded gate but not a *boot* refusal.
    s = Settings(dashscope_api_key="test", app_env="local", kinora_live_video=True)
    assert assert_safe_to_boot(s, environ={}).safe


# --------------------------------------------------------------------------- #
# Demo credentials / placeholders in prod
# --------------------------------------------------------------------------- #


def test_prod_demo_s3_credentials_refused() -> None:
    s = _prod(s3_access_key="kinora", s3_secret_key="kinora-secret")
    gate = ProdSafetyGate(environ={})
    assert "prod.demo_s3_credentials" in _fatal_codes(gate, s)
    with pytest.raises(ProdSafetyError):
        gate.assert_safe(s)


def test_prod_demo_billing_secret_refused() -> None:
    s = _prod(billing_webhook_secret="whsec_kinora_local_dev_secret")
    assert "prod.demo_billing_secret" in _fatal_codes(ProdSafetyGate(environ={}), s)


def test_prod_default_jwt_secret_refused_at_settings_layer() -> None:
    # Settings refuses the default JWT secret outside local before the gate runs.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _prod(jwt_secret="change-me-in-prod")


def test_clean_prod_boots() -> None:
    report = assert_safe_to_boot(_prod(), environ={})
    assert report.safe


# --------------------------------------------------------------------------- #
# Live video opt-in
# --------------------------------------------------------------------------- #


def test_prod_live_video_without_opt_in_refused() -> None:
    s = _prod(kinora_live_video=True)
    gate = ProdSafetyGate(environ={})
    assert "prod.live_video_without_opt_in" in _fatal_codes(gate, s)
    with pytest.raises(ProdSafetyError, match="opt-in"):
        gate.assert_safe(s)


def test_prod_live_video_with_explicit_opt_in_allowed() -> None:
    s = _prod(kinora_live_video=True)
    gate = ProdSafetyGate(environ={PROD_LIVE_VIDEO_OPT_IN_ENV: "1"})
    report = gate.assert_safe(s)
    assert report.safe


def test_prod_live_video_opt_in_must_be_truthy() -> None:
    s = _prod(kinora_live_video=True)
    gate = ProdSafetyGate(environ={PROD_LIVE_VIDEO_OPT_IN_ENV: "no"})
    assert "prod.live_video_without_opt_in" in _fatal_codes(gate, s)


def test_prod_live_video_off_never_refused() -> None:
    assert assert_safe_to_boot(_prod(kinora_live_video=False), environ={}).safe


# --------------------------------------------------------------------------- #
# Debug / chaos posture
# --------------------------------------------------------------------------- #


def test_prod_debug_logging_refused() -> None:
    s = _prod(log_level="DEBUG")
    assert "prod.debug_logging" in _fatal_codes(ProdSafetyGate(environ={}), s)


def test_prod_chaos_armed_refused() -> None:
    s = _prod()
    gate = ProdSafetyGate(environ={CHAOS_ARMED_ENV: "true"})
    assert "prod.chaos_armed" in _fatal_codes(gate, s)


def test_chaos_armed_locally_is_fine() -> None:
    s = Settings(dashscope_api_key="test", app_env="local")
    gate = ProdSafetyGate(environ={CHAOS_ARMED_ENV: "true"})
    assert gate.evaluate(s).safe


# --------------------------------------------------------------------------- #
# Validator ERROR escalation + staging
# --------------------------------------------------------------------------- #


def test_validator_error_escalated_to_fatal_in_prod() -> None:
    # An unordered watermark is a validator ERROR; in prod it must become FATAL.
    s = _prod(watermark_low_s=80, watermark_high_s=75)
    fatal = ProdSafetyGate(environ={}).evaluate(s).fatal
    codes = {f.code for f in fatal}
    assert "prod.escalated.scheduler.watermarks_unordered" in codes
    assert all(f.severity is Severity.FATAL for f in fatal)


def test_staging_is_production_grade() -> None:
    # Staging gets the same gate as prod (e.g. demo S3 creds are fatal there too).
    s = _prod(app_env="staging", s3_access_key="kinora", s3_secret_key="kinora-secret")
    assert not ProdSafetyGate(environ={}).evaluate(s).safe


def test_error_carries_all_violations() -> None:
    s = _prod(
        s3_access_key="kinora",
        s3_secret_key="kinora-secret",
        billing_webhook_secret="whsec_kinora_local_dev_secret",
        kinora_live_video=True,
    )
    with pytest.raises(ProdSafetyError) as exc:
        assert_safe_to_boot(s, environ={})
    codes = {f.code for f in exc.value.findings}
    assert {"prod.demo_s3_credentials", "prod.demo_billing_secret"} <= codes
    assert "prod.live_video_without_opt_in" in codes
    # The message lists every violation.
    assert "violation(s)" in str(exc.value)


def test_report_to_dict_shape() -> None:
    report = ProdSafetyGate(environ={}).evaluate(_prod())
    d = report.to_dict()
    assert d["safe"] is True
    assert "verdict" in d and "fatal" in d
