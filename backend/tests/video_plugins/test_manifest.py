"""Manifest + config-schema + version validation."""

from __future__ import annotations

import pytest

from app.video.plugins.config_schema import REDACTED, ConfigSchema
from app.video.plugins.contracts import RenderMode
from app.video.plugins.errors import ConfigSchemaError, ManifestError
from app.video.plugins.manifest import EntryPoint, PluginManifest
from app.video.plugins.version import Version, VersionRange

from .conftest import make_manifest_dict


def test_parse_valid_manifest_roundtrips() -> None:
    m = PluginManifest.parse(make_manifest_dict(modes=("text_to_video", "image_to_video")))
    assert m.id == "com.acme.good"
    assert m.version == Version.parse("1.0.0")
    assert RenderMode.TEXT_TO_VIDEO in m.capabilities.modes
    assert RenderMode.IMAGE_TO_VIDEO in m.capabilities.modes
    assert m.ref == "com.acme.good@1.0.0"
    # to_dict / parse round-trips identity.
    again = PluginManifest.parse(m.to_dict())
    assert again.ref == m.ref
    assert again.capabilities == m.capabilities


@pytest.mark.parametrize(
    "bad_id",
    ["", "NoDots", "UPPER.case", "trailing.", ".leading", "has space.x", "1starts.num"],
)
def test_invalid_plugin_id_rejected(bad_id: str) -> None:
    with pytest.raises(ManifestError):
        PluginManifest.parse(make_manifest_dict(plugin_id=bad_id))


@pytest.mark.parametrize("bad_entry", ["nocolon", ":noattr", "mod:", "mod:1bad", "a b:c"])
def test_invalid_entry_point_rejected(bad_entry: str) -> None:
    with pytest.raises(ManifestError):
        PluginManifest.parse(make_manifest_dict(entry_point=bad_entry))


def test_entry_point_parse_and_str() -> None:
    ep = EntryPoint.parse("pkg.sub:create")
    assert (ep.module, ep.attr) == ("pkg.sub", "create")
    assert str(ep) == "pkg.sub:create"


def test_empty_modes_rejected() -> None:
    with pytest.raises(ManifestError):
        PluginManifest.parse(make_manifest_dict(modes=()))


def test_unknown_mode_rejected() -> None:
    with pytest.raises(ManifestError):
        PluginManifest.parse(make_manifest_dict(modes=("teleport_to_video",)))


def test_bad_import_allowlist_module_rejected() -> None:
    with pytest.raises(ManifestError):
        PluginManifest.parse(make_manifest_dict(import_allowlist=["not a module!"]))


def test_resource_limits_clamped_to_ceiling() -> None:
    m = PluginManifest.parse(
        make_manifest_dict(resource_limits={"wall_time_ms": 10_000_000, "max_host_calls": 99_999})
    )
    # Clamped down to the host ceiling (120_000 / 256).
    assert m.resource_limits.wall_time_ms == 120_000
    assert m.resource_limits.max_host_calls == 256


def test_resource_limits_reject_non_positive() -> None:
    with pytest.raises(ManifestError):
        PluginManifest.parse(make_manifest_dict(resource_limits={"wall_time_ms": 0}))


# --- config schema --------------------------------------------------------- #


def test_config_schema_validates_required_and_default() -> None:
    schema = ConfigSchema.from_iterable(
        [
            {"name": "api_key", "type": "str", "required": True, "secret": True},
            {"name": "timeout", "type": "int", "default": 30},
        ]
    )
    resolved = schema.validate({"api_key": "sk-123"})
    assert resolved == {"api_key": "sk-123", "timeout": 30}
    # Secrets redacted for logging.
    assert schema.redact(resolved) == {"api_key": REDACTED, "timeout": 30}
    assert schema.secret_fields == frozenset({"api_key"})


def test_config_missing_required_field_raises() -> None:
    schema = ConfigSchema.from_iterable([{"name": "api_key", "type": "str", "required": True}])
    with pytest.raises(ConfigSchemaError) as exc:
        schema.validate({})
    assert exc.value.field == "api_key"


def test_config_unknown_field_rejected() -> None:
    schema = ConfigSchema.from_iterable([{"name": "x", "type": "int", "default": 1}])
    with pytest.raises(ConfigSchemaError):
        schema.validate({"typo": 5})


def test_config_type_mismatch_rejected() -> None:
    schema = ConfigSchema.from_iterable([{"name": "n", "type": "int"}])
    with pytest.raises(ConfigSchemaError):
        schema.validate({"n": "not-an-int"})
    # bool must not satisfy int.
    with pytest.raises(ConfigSchemaError):
        schema.validate({"n": True})


def test_config_required_with_default_rejected() -> None:
    with pytest.raises(ConfigSchemaError):
        ConfigSchema.from_iterable(
            [{"name": "x", "type": "int", "required": True, "default": 1}]
        )


def test_config_duplicate_field_rejected() -> None:
    with pytest.raises(ConfigSchemaError):
        ConfigSchema.from_iterable(
            [{"name": "x", "type": "int"}, {"name": "x", "type": "str"}]
        )


# --- version ranges -------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["", "1", "1.2", "1.2.3.4", "v1.2.3", "01.2.3"])
def test_invalid_version_rejected(bad: str) -> None:
    with pytest.raises(ManifestError):
        Version.parse(bad)


def test_version_ordering_and_prerelease() -> None:
    assert Version.parse("1.0.0") < Version.parse("1.0.1")
    assert Version.parse("1.0.0-rc.1") < Version.parse("1.0.0")
    assert Version.parse("2.0.0") > Version.parse("1.99.99")


@pytest.mark.parametrize(
    ("rng", "version", "expected"),
    [
        (">=1.0.0,<2.0.0", "1.5.0", True),
        (">=1.0.0,<2.0.0", "2.0.0", False),
        ("^1.2.0", "1.9.0", True),
        ("^1.2.0", "2.0.0", False),
        ("~1.2.3", "1.2.9", True),
        ("~1.2.3", "1.3.0", False),
        ("1.2.x", "1.2.7", True),
        ("1.2.x", "1.3.0", False),
        ("*", "9.9.9", True),
        (">=1.0.0,<2.0.0", "1.5.0-rc.1", False),  # prerelease excluded by default
    ],
)
def test_version_range_matching(rng: str, version: str, expected: bool) -> None:
    assert VersionRange.parse(rng).matches(version) is expected
