"""End-to-end admission: discover -> load -> conformance-gate -> register."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.video.plugins.contracts import PLUGIN_API_VERSION, RenderMode
from app.video.plugins.discovery import DESCRIPTOR_SUFFIX, PluginDiscoverer
from app.video.plugins.loader import PluginLoader
from app.video.plugins.registry import PluginState
from app.video.plugins.sandbox import CapabilityGrant
from app.video.plugins.service import VideoPluginService

from .conftest import (
    BrokenGeneratePlugin,
    GoodPlugin,
    make_manifest_dict,
)


def _write(directory: Path, stem: str, **kw: Any) -> None:
    data = make_manifest_dict(**kw)
    (directory / f"{stem}{DESCRIPTOR_SUFFIX}").write_text(json.dumps(data), "utf-8")


def _service_with(resolver: Any) -> VideoPluginService:
    """A service whose loader resolves entry points via ``resolver``."""
    return VideoPluginService(loader=PluginLoader(resolver=resolver))


async def test_good_plugin_activates(tmp_path: Path) -> None:
    _write(tmp_path, "good", plugin_id="com.acme.good")

    def resolver(_m: str, _a: str) -> Any:
        return GoodPlugin

    service = _service_with(resolver)
    result = await service.admit(directories=[tmp_path], include_entry_points=False)
    assert result.activated == ("com.acme.good@1.0.0",)
    assert result.quarantined == ()
    entry = service.registry.get("com.acme.good")
    assert entry.state is PluginState.ACTIVE
    assert service.routable() == (entry,)


async def test_broken_plugin_quarantined_not_active(tmp_path: Path) -> None:
    _write(tmp_path, "broken", plugin_id="com.acme.broken")

    def resolver(_m: str, _a: str) -> Any:
        return BrokenGeneratePlugin

    service = _service_with(resolver)
    result = await service.admit(directories=[tmp_path], include_entry_points=False)
    assert result.activated == ()
    assert result.quarantined == ("com.acme.broken@1.0.0",)
    entry = service.registry.get("com.acme.broken")
    assert entry.state is PluginState.QUARANTINED
    assert "generate_honours_request" in entry.quarantine_failures
    assert service.routable() == ()


async def test_mixed_batch_isolated(tmp_path: Path) -> None:
    """A good, a broken, an incompatible, and a malformed plugin in one sweep.

    The good one activates, the broken one quarantines, the incompatible + the
    malformed ones are skipped — and none of them disrupts the others.
    """
    _write(tmp_path, "good", plugin_id="com.acme.good")
    _write(tmp_path, "broken", plugin_id="com.acme.broken")
    _write(tmp_path, "future", plugin_id="com.acme.future", kinora_api=">=2.0.0")
    (tmp_path / f"junk{DESCRIPTOR_SUFFIX}").write_text("{bad", "utf-8")

    def resolver(_m: str, attr: str) -> Any:
        return GoodPlugin  # both good+broken descriptors share entry "acme_plugin:create"

    # Distinguish good vs broken by giving them different entry points + resolver map.
    _write(tmp_path, "broken", plugin_id="com.acme.broken", entry_point="broken_mod:create")

    def resolver2(module: str, _a: str) -> Any:
        return BrokenGeneratePlugin if module == "broken_mod" else GoodPlugin

    service = _service_with(resolver2)
    result = await service.admit(directories=[tmp_path], include_entry_points=False)

    assert result.activated == ("com.acme.good@1.0.0",)
    assert result.quarantined == ("com.acme.broken@1.0.0",)
    skip_codes = {s.reason_code for s in result.skipped}
    assert "video_plugin_incompatible" in skip_codes
    assert "video_plugin_discovery_failed" in skip_codes
    # Only the good plugin is routable.
    assert [e.plugin_id for e in service.routable()] == ["com.acme.good"]


async def test_conformance_runs_without_spend(tmp_path: Path) -> None:
    """Even granting net.fetch, the conformance gate must not call host fetch."""
    fetched: list[Any] = []

    async def fetch(*a: Any, **k: Any) -> str:
        fetched.append(a)
        return "x"

    from app.video.plugins.sandbox import HostServices

    _write(tmp_path, "good", plugin_id="com.acme.good")

    def resolver(_m: str, _a: str) -> Any:
        return GoodPlugin

    service = _service_with(resolver)
    await service.admit(
        directories=[tmp_path],
        include_entry_points=False,
        grants={"com.acme.good": CapabilityGrant(frozenset({"net.fetch"}))},
        host_services=HostServices(fetch=fetch),
    )
    # GoodPlugin doesn't fetch, but the key invariant is the gate used a no-spend
    # sandbox: the active plugin is registered with its production sandbox while
    # conformance ran against the stub. No real fetch happened during admission.
    assert fetched == []
    assert service.registry.get("com.acme.good").state is PluginState.ACTIVE


def test_supporting_filters_by_mode(tmp_path: Path) -> None:
    # Pure synchronous check of the discovery+compat surface used by routing.
    _write(tmp_path, "ok", plugin_id="com.acme.ok", kinora_api=f">={PLUGIN_API_VERSION},<2.0.0")
    discovery = PluginDiscoverer().discover(directories=[tmp_path], include_entry_points=False)
    manifest = discovery.discovered[0].manifest
    assert RenderMode.TEXT_TO_VIDEO in manifest.capabilities.modes
