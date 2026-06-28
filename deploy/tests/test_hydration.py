"""Tests for config/secret hydration + redaction + the live-video safety gate."""

from __future__ import annotations

import pytest

from deploy.orchestrator.hydration import (
    REQUIRED_RENDER_KEYS,
    SECRET_KEYS,
    DictConfigSource,
    DictSecretSource,
    HydratedConfig,
    HydrationError,
    Hydrator,
    redact_value,
)


def _complete_config() -> dict[str, str]:
    return {
        "OSS_ENDPOINT": "https://oss-ap-southeast-1.aliyuncs.com",
        "OSS_BUCKET": "kinora-assets",
    }


def _complete_secrets() -> dict[str, str]:
    return {
        "DASHSCOPE_API_KEY": "sk-test",
        "OSS_AK": "ak",
        "OSS_SECRET": "shh",
        "REDIS_URL": "redis://:pw@host:6379/0",
        "DATABASE_URL": "postgresql+asyncpg://u:pw@host:5432/db",
    }


def _hydrator(**kw: object) -> Hydrator:
    return Hydrator(
        config_source=DictConfigSource(kw.pop("config", _complete_config())),  # type: ignore[arg-type]
        secret_source=DictSecretSource(kw.pop("secrets", _complete_secrets())),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def test_hydrate_merges_config_and_secrets() -> None:
    cfg = _hydrator().hydrate()
    assert cfg.get("OSS_BUCKET") == "kinora-assets"
    assert cfg.get("DASHSCOPE_API_KEY") == "sk-test"
    assert cfg.live_video_enabled is False
    # The gate is normalised to an explicit value.
    assert cfg.get("KINORA_LIVE_VIDEO") == "false"


def test_missing_required_key_fails_fast() -> None:
    secrets = _complete_secrets()
    del secrets["OSS_AK"]
    with pytest.raises(HydrationError) as exc:
        _hydrator(secrets=secrets).hydrate()
    assert "OSS_AK" in str(exc.value)


def test_required_render_keys_cover_oss_dashscope_redis_db() -> None:
    expected = (
        "DASHSCOPE_API_KEY",
        "OSS_ENDPOINT",
        "OSS_AK",
        "OSS_SECRET",
        "OSS_BUCKET",
        "REDIS_URL",
        "DATABASE_URL",
    )
    for key in expected:
        assert key in REQUIRED_RENDER_KEYS


def test_live_video_on_without_permission_is_refused() -> None:
    cfg = _complete_config()
    cfg["KINORA_LIVE_VIDEO"] = "true"
    with pytest.raises(HydrationError) as exc:
        _hydrator(config=cfg, allow_live_video=False).hydrate()
    assert "KINORA_LIVE_VIDEO" in str(exc.value)


def test_live_video_on_with_permission_is_allowed() -> None:
    cfg = _complete_config()
    cfg["KINORA_LIVE_VIDEO"] = "1"
    hydrated = _hydrator(config=cfg, allow_live_video=True).hydrate()
    assert hydrated.live_video_enabled is True


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE", "On"])
def test_live_video_truthy_variants(value: str) -> None:
    cfg = _complete_config()
    cfg["KINORA_LIVE_VIDEO"] = value
    with pytest.raises(HydrationError):
        _hydrator(config=cfg, allow_live_video=False).hydrate()


def test_redacted_view_hides_secrets() -> None:
    cfg = _hydrator().hydrate()
    redacted = cfg.redacted()
    assert redacted["DASHSCOPE_API_KEY"] == "***"
    assert redacted["REDIS_URL"] == "***"
    assert redacted["DATABASE_URL"] == "***"
    # Non-secrets are visible.
    assert redacted["OSS_BUCKET"] == "kinora-assets"


def test_redact_value_helper() -> None:
    assert redact_value("DASHSCOPE_API_KEY", "sk-real") == "***"
    assert redact_value("OSS_BUCKET", "kinora-assets") == "kinora-assets"


def test_fingerprint_stable_and_secret_free() -> None:
    a = _hydrator().hydrate()
    b = _hydrator().hydrate()
    assert a.fingerprint() == b.fingerprint()
    # Changing a secret value does not change the fingerprint (values redacted),
    # but changing a non-secret value does.
    secrets = _complete_secrets()
    secrets["DASHSCOPE_API_KEY"] = "sk-different"
    c = _hydrator(secrets=secrets).hydrate()
    assert c.fingerprint() == a.fingerprint()

    cfg = _complete_config()
    cfg["OSS_BUCKET"] = "other-bucket"
    d = _hydrator(config=cfg).hydrate()
    assert d.fingerprint() != a.fingerprint()


def test_secret_keys_constant_includes_credentials() -> None:
    assert "DASHSCOPE_API_KEY" in SECRET_KEYS
    assert "OSS_SECRET" in SECRET_KEYS
    assert "DATABASE_URL" in SECRET_KEYS


def test_hydrated_config_direct_construction_redacts_by_secret_set() -> None:
    cfg = HydratedConfig(
        values={"DASHSCOPE_API_KEY": "sk", "OSS_BUCKET": "b"},
        secret_keys=frozenset({"DASHSCOPE_API_KEY"}),
    )
    assert cfg.redacted() == {"DASHSCOPE_API_KEY": "***", "OSS_BUCKET": "b"}
