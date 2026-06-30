"""Discovery: entry-point + directory sources, version-compat, graceful skips."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.video.plugins.contracts import PLUGIN_API_VERSION
from app.video.plugins.discovery import (
    DESCRIPTOR_SUFFIX,
    PluginDiscoverer,
)
from app.video.plugins.errors import IncompatiblePluginError

from .conftest import make_manifest_dict


@dataclass
class FakeEntryPoint:
    """A stand-in for an importlib.metadata entry point."""

    name: str
    target: Any

    def load(self) -> Any:
        if isinstance(self.target, Exception):
            raise self.target
        return self.target


def _write_descriptor(directory: Path, stem: str, data: dict[str, Any]) -> Path:
    path = directory / f"{stem}{DESCRIPTOR_SUFFIX}"
    path.write_text(json.dumps(data), "utf-8")
    return path


# --- directory discovery --------------------------------------------------- #


def test_directory_discovery_finds_descriptors(tmp_path: Path) -> None:
    _write_descriptor(tmp_path, "a", make_manifest_dict(plugin_id="com.acme.a"))
    _write_descriptor(tmp_path, "b", make_manifest_dict(plugin_id="com.acme.b"))
    result = PluginDiscoverer().discover(directories=[tmp_path], include_entry_points=False)
    assert {d.manifest.id for d in result.discovered} == {"com.acme.a", "com.acme.b"}
    assert result.skipped == ()


def test_malformed_descriptor_is_skipped_not_raised(tmp_path: Path) -> None:
    (tmp_path / f"bad{DESCRIPTOR_SUFFIX}").write_text("{not json", "utf-8")
    _write_descriptor(tmp_path, "good", make_manifest_dict(plugin_id="com.acme.good"))
    result = PluginDiscoverer().discover(directories=[tmp_path], include_entry_points=False)
    # The good one still loads; the bad one is a recorded skip.
    assert [d.manifest.id for d in result.discovered] == ["com.acme.good"]
    assert len(result.skipped) == 1
    assert result.skipped[0].reason_code == "video_plugin_discovery_failed"


def test_invalid_manifest_in_descriptor_is_skipped(tmp_path: Path) -> None:
    _write_descriptor(tmp_path, "bad", make_manifest_dict(plugin_id="BADID"))
    result = PluginDiscoverer().discover(directories=[tmp_path], include_entry_points=False)
    assert result.discovered == ()
    assert result.skipped[0].reason_code == "video_plugin_manifest_invalid"


def test_missing_directory_is_a_no_op(tmp_path: Path) -> None:
    result = PluginDiscoverer().discover(
        directories=[tmp_path / "does-not-exist"], include_entry_points=False
    )
    assert result.discovered == () and result.skipped == ()


# --- version compatibility ------------------------------------------------- #


def test_incompatible_plugin_skipped_gracefully(tmp_path: Path) -> None:
    # Targets a future major the host (1.x) does not satisfy.
    _write_descriptor(
        tmp_path, "future", make_manifest_dict(plugin_id="com.acme.future", kinora_api=">=2.0.0")
    )
    result = PluginDiscoverer().discover(directories=[tmp_path], include_entry_points=False)
    assert result.discovered == ()
    assert len(result.skipped) == 1
    skip = result.skipped[0]
    assert skip.reason_code == IncompatiblePluginError.code
    assert skip.plugin_id == "com.acme.future"


def test_compatible_plugin_accepted(tmp_path: Path) -> None:
    _write_descriptor(
        tmp_path,
        "ok",
        make_manifest_dict(plugin_id="com.acme.ok", kinora_api=f">={PLUGIN_API_VERSION},<2.0.0"),
    )
    result = PluginDiscoverer().discover(directories=[tmp_path], include_entry_points=False)
    assert [d.manifest.id for d in result.discovered] == ["com.acme.ok"]


def test_host_api_override_changes_compat() -> None:
    # A plugin pinned to >=2 is incompatible with host 1, compatible with host 2.
    manifest = make_manifest_dict(plugin_id="com.acme.v2", kinora_api=">=2.0.0,<3.0.0")
    ep = FakeEntryPoint("v2", manifest)
    host1 = PluginDiscoverer(host_api="1.0.0").discover(entry_points=[ep])
    host2 = PluginDiscoverer(host_api="2.1.0").discover(entry_points=[ep])
    assert host1.discovered == () and len(host1.skipped) == 1
    assert [d.manifest.id for d in host2.discovered] == ["com.acme.v2"]


# --- entry-point discovery ------------------------------------------------- #


def test_entry_point_dict_target_discovered() -> None:
    ep = FakeEntryPoint("acme", make_manifest_dict(plugin_id="com.acme.ep"))
    result = PluginDiscoverer().discover(entry_points=[ep])
    assert [d.manifest.id for d in result.discovered] == ["com.acme.ep"]
    assert result.discovered[0].source == "entry-point:acme"


def test_entry_point_manifest_attribute_discovered() -> None:
    class Pkg:
        MANIFEST = make_manifest_dict(plugin_id="com.acme.attr")

    ep = FakeEntryPoint("attr", Pkg)
    result = PluginDiscoverer().discover(entry_points=[ep])
    assert [d.manifest.id for d in result.discovered] == ["com.acme.attr"]


def test_entry_point_that_raises_on_load_is_skipped() -> None:
    ep = FakeEntryPoint("broken", ImportError("boom"))
    result = PluginDiscoverer().discover(entry_points=[ep])
    assert result.discovered == ()
    assert result.skipped[0].reason_code == "video_plugin_discovery_failed"


def test_entry_point_without_manifest_is_skipped() -> None:
    ep = FakeEntryPoint("nomanifest", object())
    result = PluginDiscoverer().discover(entry_points=[ep])
    assert result.discovered == ()
    assert len(result.skipped) == 1


# --- duplicate / shadowing ------------------------------------------------- #


def test_higher_version_shadows_lower(tmp_path: Path) -> None:
    _write_descriptor(tmp_path, "v1", make_manifest_dict(plugin_id="com.acme.dup", version="1.0.0"))
    _write_descriptor(tmp_path, "v2", make_manifest_dict(plugin_id="com.acme.dup", version="1.5.0"))
    result = PluginDiscoverer().discover(directories=[tmp_path], include_entry_points=False)
    assert len(result.discovered) == 1
    assert str(result.discovered[0].manifest.version) == "1.5.0"
    assert any(s.reason_code == "video_plugin_shadowed" for s in result.skipped)


def test_both_sources_combined(tmp_path: Path) -> None:
    _write_descriptor(tmp_path, "dir", make_manifest_dict(plugin_id="com.acme.dir"))
    ep = FakeEntryPoint("ep", make_manifest_dict(plugin_id="com.acme.ep"))
    result = PluginDiscoverer().discover(directories=[tmp_path], entry_points=[ep])
    assert {d.manifest.id for d in result.discovered} == {"com.acme.dir", "com.acme.ep"}
