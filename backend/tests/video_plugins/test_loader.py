"""Loader: config validation, secret injection, sandboxed instantiation."""

from __future__ import annotations

from typing import Any

import pytest

from app.video.plugins.contracts import ProbeResult, VideoArtifact, VideoRequest
from app.video.plugins.errors import ConfigSchemaError, PluginRuntimeError
from app.video.plugins.loader import PluginLoader
from app.video.plugins.sandbox import CapabilityGrant, HostServices

from .conftest import GoodPlugin, make_manifest, resolver_for

_API_KEY_SCHEMA = [
    {"name": "api_key", "type": "str", "required": True, "secret": True},
    {"name": "region", "type": "str", "default": "intl"},
]


def test_load_validates_config_against_schema() -> None:
    manifest = make_manifest(config_schema=_API_KEY_SCHEMA)
    loader = PluginLoader(resolver=resolver_for(GoodPlugin))
    # Missing the required secret ⇒ rejected before any plugin code runs.
    with pytest.raises(ConfigSchemaError):
        loader.load(manifest, config={}, grant=CapabilityGrant(frozenset()))


def test_load_injects_only_declared_secrets() -> None:
    seen: dict[str, Any] = {}

    class CapturePlugin(GoodPlugin):
        def __init__(self, *, config: dict[str, Any], host: object) -> None:
            super().__init__(config=config, host=host)
            seen["config"] = config
            seen["key"] = host.host_secret("api_key")  # type: ignore[attr-defined]

    manifest = make_manifest(config_schema=_API_KEY_SCHEMA)
    loader = PluginLoader(resolver=resolver_for(CapturePlugin))
    loaded = loader.load(
        manifest,
        config={"api_key": "sk-secret"},
        grant=CapabilityGrant(frozenset({"host.secret"})),
        services=HostServices(),
    )
    assert seen["config"] == {"api_key": "sk-secret", "region": "intl"}
    assert seen["key"] == "sk-secret"
    # The resolved config keeps the value; redaction masks it for logging.
    assert loaded.config["api_key"] == "sk-secret"
    assert manifest.config_schema.redact(loaded.config)["api_key"] == "***"


def test_load_unresolvable_entry_point_contained() -> None:
    def bad_resolver(_m: str, _a: str) -> Any:
        raise ImportError("no such module")

    manifest = make_manifest()
    loader = PluginLoader(resolver=bad_resolver)
    with pytest.raises(PluginRuntimeError):
        loader.load(manifest, config={}, grant=CapabilityGrant(frozenset()))


async def test_loaded_plugin_drives_through_its_sandbox() -> None:
    manifest = make_manifest()
    loader = PluginLoader(resolver=resolver_for(GoodPlugin))
    loaded = loader.load(manifest, config={}, grant=CapabilityGrant(frozenset()))
    call = await loaded.sandbox.probe(loaded.instance)
    assert isinstance(call.value, ProbeResult)
    mode = next(iter(loaded.manifest.capabilities.modes))
    gen = await loaded.sandbox.generate(loaded.instance, VideoRequest(mode=mode))
    assert isinstance(gen.value, VideoArtifact)
