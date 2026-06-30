"""Scaffolding: create_plugin_template emits a valid, conformance-passing plugin."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.video.plugins.contracts import RenderMode
from app.video.plugins.errors import ManifestError
from app.video.plugins.manifest import PluginManifest
from app.video.plugins.scaffold import create_plugin_template, write_plugin_template


def test_scaffold_descriptor_parses() -> None:
    scaffold = create_plugin_template(
        plugin_id="com.acme.newvid",
        name="NewVid",
        modes=(RenderMode.TEXT_TO_VIDEO, RenderMode.IMAGE_TO_VIDEO),
    )
    # The returned manifest already parsed; re-parse the JSON for good measure.
    import json

    reparsed = PluginManifest.parse(json.loads(scaffold.descriptor_json))
    assert reparsed.id == "com.acme.newvid"
    assert RenderMode.IMAGE_TO_VIDEO in reparsed.capabilities.modes
    assert scaffold.descriptor_filename == "com_acme_newvid.plugin.json"
    assert scaffold.module_filename == "kinora_plugin.py"


def test_scaffold_reference_mode_sets_reference_images() -> None:
    scaffold = create_plugin_template(
        plugin_id="com.acme.refvid",
        name="RefVid",
        modes=(RenderMode.REFERENCE_TO_VIDEO,),
    )
    assert scaffold.manifest.capabilities.max_reference_images > 0


def test_scaffold_invalid_id_raises() -> None:
    with pytest.raises(ManifestError):
        create_plugin_template(plugin_id="NOTVALID", name="x")


def test_scaffold_module_is_valid_python_and_factory_works() -> None:
    scaffold = create_plugin_template(plugin_id="com.acme.gen", name="Gen", needs_api_key=False)
    ns: dict[str, object] = {}
    exec(compile(scaffold.module_source, "<scaffold>", "exec"), ns)  # noqa: S102 - test of generated code
    factory = ns["create"]
    assert callable(factory)
    plugin = factory(config={}, host=object())
    assert plugin.capabilities is not None


async def test_scaffold_module_passes_conformance() -> None:
    from app.video.plugins.conformance import ConformanceHarness
    from app.video.plugins.limits import ResourceLimits
    from app.video.plugins.sandbox import CapabilityGrant, HostServices, Sandbox

    scaffold = create_plugin_template(plugin_id="com.acme.conf", name="Conf", needs_api_key=False)
    ns: dict[str, object] = {}
    exec(compile(scaffold.module_source, "<scaffold>", "exec"), ns)  # noqa: S102
    plugin = ns["create"](config={}, host=object())  # type: ignore[operator]
    sandbox = Sandbox(
        plugin_id=scaffold.manifest.id,
        grant=CapabilityGrant(frozenset()),
        services=HostServices(),
        limits=ResourceLimits(),
    )
    report = await ConformanceHarness().run(
        plugin_ref=scaffold.manifest.ref,
        plugin=plugin,
        profile=scaffold.manifest.capabilities,
        sandbox=sandbox,
    )
    assert report.passed, report.failures


def test_write_plugin_template_materializes(tmp_path: Path) -> None:
    scaffold = create_plugin_template(plugin_id="com.acme.disk", name="Disk")
    descriptor_path, module_path = write_plugin_template(tmp_path, scaffold)
    assert descriptor_path.exists() and module_path.exists()
    assert descriptor_path.name == "com_acme_disk.plugin.json"
    # The materialized descriptor is discoverable.
    from app.video.plugins.discovery import PluginDiscoverer

    result = PluginDiscoverer().discover(directories=[tmp_path], include_entry_points=False)
    assert [d.manifest.id for d in result.discovered] == ["com.acme.disk"]
