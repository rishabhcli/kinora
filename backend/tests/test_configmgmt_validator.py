"""Cross-field config invariants + readiness verdict (app.configmgmt.validator).

Pure — no infra, no network, never enables KINORA_LIVE_VIDEO live spend (the
gate is only ever *inspected* here). Each invariant is asserted to fire on a
crafted Settings and to pass on a clean one, and the readiness roll-up is checked.
"""

from __future__ import annotations

from app.configmgmt.errors import Severity
from app.configmgmt.validator import (
    INVARIANTS,
    ConfigValidator,
    ReadinessVerdict,
    validate_settings,
)
from app.core.config import Settings

_REAL = "a-real-production-secret-value-32+bytes"


def _prod(**overrides: object) -> Settings:
    """A clean, bootable prod Settings; override individual knobs per test."""
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


def _codes(verdict: ReadinessVerdict) -> set[str]:
    return {f.code for f in verdict.findings}


# --------------------------------------------------------------------------- #
# Clean baselines
# --------------------------------------------------------------------------- #


def test_local_default_is_ready() -> None:
    v = validate_settings(Settings(dashscope_api_key="test", app_env="local"))
    assert v.is_ready
    # Only the (advisory) default-secret INFOs locally; nothing blocking.
    assert v.errors == ()
    assert all(f.severity <= Severity.INFO for f in v.findings)


def test_clean_prod_is_ready() -> None:
    v = validate_settings(_prod())
    assert v.is_ready, [str(f) for f in v.errors]
    assert v.env == "prod"


# --------------------------------------------------------------------------- #
# Live-video gate (guarded; never enabled by this plane)
# --------------------------------------------------------------------------- #


def test_live_video_off_emits_no_live_findings() -> None:
    v = validate_settings(_prod(kinora_live_video=False))
    assert not any(c.startswith("live_video.") for c in _codes(v))


def test_live_video_armed_warns_and_requires_usd_cap() -> None:
    v = validate_settings(_prod(kinora_live_video=True, budget_ceiling_usd=0.0))
    codes = _codes(v)
    assert "live_video.armed" in codes  # unconditional warning when armed
    assert "live_video.no_usd_cap" in codes
    assert not v.is_ready  # the missing cap is an ERROR


def test_live_video_armed_with_caps_is_only_a_warning() -> None:
    v = validate_settings(_prod(kinora_live_video=True))  # caps default positive
    codes = _codes(v)
    assert "live_video.armed" in codes
    assert "live_video.no_usd_cap" not in codes
    assert v.is_ready  # armed-but-guarded does not block readiness


def test_live_video_minimax_without_key_is_error() -> None:
    v = validate_settings(
        _prod(kinora_live_video=True, video_backend="minimax", minimax_api_key=None)
    )
    assert "live_video.minimax_no_key" in _codes(v)
    assert not v.is_ready


# --------------------------------------------------------------------------- #
# Video backend
# --------------------------------------------------------------------------- #


def test_unknown_video_backend_is_error() -> None:
    v = validate_settings(_prod(video_backend="sora"))
    assert "video_backend.unknown" in _codes(v)
    assert not v.is_ready


def test_minimax_backend_without_key_warns_when_not_live() -> None:
    v = validate_settings(_prod(video_backend="minimax", minimax_api_key=None))
    findings = {f.code: f for f in v.findings}
    assert "video_backend.minimax_no_key" in findings
    # Not live => warning (not blocking).
    assert findings["video_backend.minimax_no_key"].severity is Severity.WARNING
    assert v.is_ready


def test_minimax_backend_with_key_is_clean() -> None:
    v = validate_settings(_prod(video_backend="minimax", minimax_api_key="mk-real"))
    assert "video_backend.minimax_no_key" not in _codes(v)


# --------------------------------------------------------------------------- #
# Reasoning provider
# --------------------------------------------------------------------------- #


def test_reasoning_openai_requires_key_is_reported_as_finding() -> None:
    # Settings itself raises for this; build via dashscope then mutate the field
    # so the validator's own finding path is exercised.
    s = _prod()
    object.__setattr__(s, "reasoning_provider", "openai")
    object.__setattr__(s, "openai_api_key", None)
    v = validate_settings(s)
    assert "reasoning.openai_no_key" in _codes(v)


def test_reasoning_unknown_provider_is_reported() -> None:
    s = _prod()
    object.__setattr__(s, "reasoning_provider", "bedrock")
    v = validate_settings(s)
    assert "reasoning.unknown_provider" in _codes(v)


# --------------------------------------------------------------------------- #
# S3 coherence
# --------------------------------------------------------------------------- #


def test_s3_missing_public_url_outside_local_warns() -> None:
    v = validate_settings(_prod(s3_public_base_url=None))
    assert "s3.no_public_base_url" in _codes(v)
    assert v.is_ready  # warning, not blocking


def test_s3_localhost_public_url_outside_local_warns() -> None:
    v = validate_settings(_prod(s3_public_base_url="http://localhost:9000/kinora"))
    assert "s3.localhost_public_url" in _codes(v)


def test_s3_empty_bucket_is_error() -> None:
    v = validate_settings(_prod(s3_bucket="  "))
    assert "s3.no_bucket" in _codes(v)
    assert not v.is_ready


# --------------------------------------------------------------------------- #
# Scheduler / budget / finops ordering
# --------------------------------------------------------------------------- #


def test_unordered_watermarks_is_error() -> None:
    v = validate_settings(_prod(watermark_low_s=80, watermark_high_s=75))
    assert "scheduler.watermarks_unordered" in _codes(v)
    assert not v.is_ready


def test_unordered_horizons_is_error() -> None:
    v = validate_settings(_prod(commit_horizon_s=300, spec_horizon_s=240))
    assert "scheduler.horizons_unordered" in _codes(v)


def test_session_budget_exceeding_ceiling_warns() -> None:
    v = validate_settings(_prod(budget_per_session_s=5000, budget_ceiling_video_s=1650))
    assert "budget.session_exceeds_ceiling" in _codes(v)


def test_finops_unordered_fractions_is_error() -> None:
    v = validate_settings(
        _prod(
            finops_alert_info_fraction=0.9,
            finops_alert_warning_fraction=0.5,
            finops_soft_cap_fraction=0.7,
        )
    )
    assert "finops.fractions_unordered" in _codes(v)
    assert not v.is_ready


def test_finops_ordered_fractions_pass() -> None:
    v = validate_settings(_prod())  # defaults 0.5/0.75/0.9 are valid
    assert "finops.fractions_unordered" not in _codes(v)


# --------------------------------------------------------------------------- #
# Misc invariants
# --------------------------------------------------------------------------- #


def test_bad_embed_dim_is_error() -> None:
    v = validate_settings(_prod(embed_dim=0))
    assert "embed.bad_dim" in _codes(v)


def test_unknown_log_level_warns() -> None:
    v = validate_settings(_prod(log_level="CHATTY"))
    assert "logging.bad_level" in _codes(v)


def test_wildcard_cors_outside_local_warns() -> None:
    v = validate_settings(_prod(cors_origins=["*"]))
    assert "cors.wildcard" in _codes(v)


def test_unauthenticated_mcp_outside_local_is_error() -> None:
    v = validate_settings(_prod(mcp_auth_token=None))
    assert "mcp.unauthenticated" in _codes(v)
    assert not v.is_ready


def test_default_jwt_outside_local_is_error_finding() -> None:
    # Settings would raise; mutate a clean prod Settings to exercise the finding.
    s = _prod()
    from app.core.config import DEFAULT_JWT_SECRET

    object.__setattr__(s, "jwt_secret", DEFAULT_JWT_SECRET)
    v = validate_settings(s)
    f = {x.code: x for x in v.findings}["secrets.default_jwt"]
    assert f.severity is Severity.ERROR


# --------------------------------------------------------------------------- #
# Readiness verdict roll-up
# --------------------------------------------------------------------------- #


def test_verdict_max_severity_and_counts() -> None:
    v = validate_settings(_prod(video_backend="bogus", log_level="LOUD"))
    assert v.max_severity is Severity.ERROR
    d = v.to_dict()
    assert d["ready"] is False
    assert d["env"] == "prod"
    counts = d["counts"]
    assert isinstance(counts, dict)
    assert counts["error"] >= 1
    assert counts["warning"] >= 1


def test_verdict_no_findings_when_pristine_local() -> None:
    # A local Settings with explicit non-default secrets => zero findings.
    v = validate_settings(
        Settings(
            dashscope_api_key="k",
            app_env="local",
            jwt_secret=_REAL,
            api_key_pepper=_REAL + "p",
        )
    )
    assert v.findings == ()
    assert v.max_severity is None
    assert v.is_ready


def test_custom_invariant_suite_runs_only_given_checks() -> None:
    # The validator honours an injected suite (composability).
    only = (INVARIANTS[0],)  # live-video only
    cv = ConfigValidator(invariants=only)
    v = cv.validate(_prod(video_backend="bogus"))
    # The unknown-backend check is NOT in the suite, so it cannot fire.
    assert "video_backend.unknown" not in _codes(v)
