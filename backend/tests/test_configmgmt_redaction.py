"""redacted_dump + secret-field classification (app.configmgmt.redaction).

Pure — no infra. Asserts every known secret field is masked while the dump stays
structurally faithful, and that None secrets stay None.
"""

from __future__ import annotations

from app.configmgmt.redaction import (
    ALWAYS_REDACT_FIELDS,
    is_secret_field,
    redact_mapping,
    redacted_dump,
)
from app.core.config import Settings
from app.core.logging import REDACTED

_REAL = "a-real-production-secret-value-32+bytes"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"dashscope_api_key": "live-key-123", "app_env": "local"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_is_secret_field_name_heuristic() -> None:
    assert is_secret_field("jwt_secret")
    assert is_secret_field("minimax_api_key")
    assert is_secret_field("mcp_auth_token")
    assert is_secret_field("s3_secret_key")  # explicit set
    assert is_secret_field("s3_access_key")  # explicit set (name has no marker)
    assert not is_secret_field("log_level")
    assert not is_secret_field("video_backend")


def test_dump_masks_all_secret_fields() -> None:
    dump = redacted_dump(_settings(jwt_secret=_REAL, api_key_pepper=_REAL + "p"))
    for field in ("dashscope_api_key", "jwt_secret", "api_key_pepper", "s3_secret_key"):
        assert dump[field] == REDACTED, field
    # A non-secret stays visible.
    assert dump["app_env"] == "local"
    assert dump["video_backend"] == "dashscope"


def test_dump_does_not_leak_the_live_dashscope_key() -> None:
    dump = redacted_dump(_settings(dashscope_api_key="sk-super-secret-live"))
    assert "sk-super-secret-live" not in repr(dump)
    assert dump["dashscope_api_key"] == REDACTED


def test_none_secret_stays_none() -> None:
    # An unset optional secret stays None so "unset" != "hidden".
    dump = redacted_dump(_settings(minimax_api_key=None, openai_api_key=None))
    assert dump["minimax_api_key"] is None
    assert dump["openai_api_key"] is None


def test_dump_is_structurally_complete() -> None:
    s = _settings()
    dump = redacted_dump(s)
    # Every model field is present in the dump.
    assert set(dump) == set(s.model_dump())


def test_redact_mapping_masks_token_keys() -> None:
    # A nested map carrying secret-named keys (reuses the app.core.logging
    # vocabulary: ``token`` exact, ``access_token``/``api_key`` substrings).
    nested = {"token": "tok123", "api_key": "ak1", "subject": "judge", "scopes": ["read"]}
    out = redact_mapping(nested)
    assert out["token"] == REDACTED
    assert out["api_key"] == REDACTED
    assert out["subject"] == "judge"
    assert out["scopes"] == ["read"]


def test_always_redact_fields_are_all_real_settings_fields() -> None:
    # Guard against a typo'd field name in the explicit redaction set.
    fields = set(Settings.model_fields)
    assert fields >= ALWAYS_REDACT_FIELDS, ALWAYS_REDACT_FIELDS - fields
