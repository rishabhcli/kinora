"""Scaffolding / codegen for a new video-provider plugin.

The fastest way to make the plugin contract real for a third-party author is to
*generate a working plugin for them*. :func:`create_plugin_template` emits a
self-contained starter — a validated manifest descriptor plus a Python module
whose factory returns a contract-satisfying, conformance-passing stub — so an
author runs one call, gets a green plugin, and then fills in the real model API.

The generator is pure (returns the file contents as strings) with an optional
:func:`write_plugin_template` that materializes them to disk. The emitted
manifest is round-tripped through :meth:`PluginManifest.parse` before it is
returned, so the scaffold can never produce a descriptor the SDK would reject.
The emitted module is written to *pass the default conformance contract*: its
``generate`` echoes the requested mode and returns a positive-duration artifact,
and its ``probe`` returns healthy — exactly what an author needs as a baseline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.video.plugins.contracts import PLUGIN_API_VERSION, RenderMode
from app.video.plugins.discovery import DESCRIPTOR_SUFFIX
from app.video.plugins.manifest import PluginManifest


@dataclass(frozen=True, slots=True)
class ScaffoldedPlugin:
    """The generated artifacts for a new plugin."""

    #: The descriptor filename (``<slug>.plugin.json``).
    descriptor_filename: str
    #: The pretty-printed manifest JSON.
    descriptor_json: str
    #: The starter Python module source (the factory + a contract-satisfying stub).
    module_filename: str
    module_source: str
    #: The validated manifest (proof the descriptor parses).
    manifest: PluginManifest


def create_plugin_template(
    *,
    plugin_id: str,
    name: str,
    publisher: str = "",
    modes: tuple[RenderMode, ...] = (RenderMode.TEXT_TO_VIDEO,),
    entry_module: str = "kinora_plugin",
    factory_attr: str = "create",
    needs_api_key: bool = True,
) -> ScaffoldedPlugin:
    """Generate a working plugin scaffold for ``plugin_id``.

    Args:
        plugin_id: a reverse-DNS slug (e.g. ``com.acme.lumavid``).
        name: human-readable plugin name.
        publisher: optional publisher string.
        modes: the render modes the stub advertises (defaults to t2v).
        entry_module: the module name the factory lives in.
        factory_attr: the factory attribute name in that module.
        needs_api_key: when True, the manifest declares a secret ``api_key``
            config field and the stub reads it through the host handle.

    Returns:
        A :class:`ScaffoldedPlugin` with the descriptor + module source.

    Raises:
        ManifestError: if the requested identity would not produce a valid
            manifest (so the scaffold never emits something the SDK rejects).
    """
    config_schema: list[dict[str, object]] = []
    if needs_api_key:
        config_schema.append(
            {
                "name": "api_key",
                "type": "str",
                "required": True,
                "secret": True,
                "description": "Credential for the model provider's API.",
            }
        )
        config_schema.append(
            {
                "name": "base_url",
                "type": "str",
                "required": False,
                "default": "https://api.example.invalid/v1",
                "description": "Provider API base URL.",
            }
        )

    descriptor: dict[str, object] = {
        "id": plugin_id,
        "version": "0.1.0",
        "name": name,
        "publisher": publisher,
        "kinora_api": f">={PLUGIN_API_VERSION},<2.0.0",
        "entry_point": f"{entry_module}:{factory_attr}",
        "capabilities": {
            "modes": sorted(m.value for m in modes),
            "resolutions": ["720P"],
            "min_duration_s": 1.0,
            "max_duration_s": 10.0,
            "supports_negative_prompt": True,
            "supports_seed": True,
            "supports_audio": False,
            "max_reference_images": 4 if RenderMode.REFERENCE_TO_VIDEO in modes else 0,
        },
        "config_schema": config_schema,
        "resource_limits": {"wall_time_ms": 60_000, "max_host_calls": 32},
        "import_allowlist": [],
        "description": f"A Kinora video-provider plugin for {name}.",
    }

    # Round-trip through parse so a scaffold never emits an invalid descriptor.
    manifest = PluginManifest.parse(descriptor)

    slug = plugin_id.replace(".", "_")
    descriptor_filename = f"{slug}{DESCRIPTOR_SUFFIX}"
    module_filename = f"{entry_module}.py"
    module_source = _MODULE_TEMPLATE.format(
        name=name,
        plugin_id=plugin_id,
        factory_attr=factory_attr,
        modes_repr=", ".join(f"RenderMode.{m.name}" for m in modes),
        reads_secret="True" if needs_api_key else "False",
    )

    return ScaffoldedPlugin(
        descriptor_filename=descriptor_filename,
        descriptor_json=json.dumps(descriptor, indent=2, sort_keys=True),
        module_filename=module_filename,
        module_source=module_source,
        manifest=manifest,
    )


def write_plugin_template(target_dir: Path, scaffold: ScaffoldedPlugin) -> tuple[Path, Path]:
    """Materialize a scaffold to ``target_dir`` (descriptor + module). Returns the paths."""
    target_dir.mkdir(parents=True, exist_ok=True)
    descriptor_path = target_dir / scaffold.descriptor_filename
    module_path = target_dir / scaffold.module_filename
    descriptor_path.write_text(scaffold.descriptor_json + "\n", "utf-8")
    module_path.write_text(scaffold.module_source, "utf-8")
    return descriptor_path, module_path


#: The starter module. The factory returns a stub that satisfies
#: :class:`~app.video.plugins.contracts.VideoProviderPlugin` and passes the
#: default conformance contract; the author replaces the body of ``generate``
#: with a real call to their model's API (through ``host.fetch``).
_MODULE_TEMPLATE = '''"""Kinora video-provider plugin: {name} ({plugin_id}).

Generated by ``app.video.plugins.scaffold.create_plugin_template``. Replace the
body of ``MyVideoPlugin.generate`` with a real call to your model API via
``self._host.fetch(...)`` and persist nothing yourself — return a ``VideoArtifact``
referencing the produced clip; the Kinora host downloads + stores it.
"""

from __future__ import annotations

from app.video.plugins.contracts import (
    CapabilityProfile,
    ProbeResult,
    RenderMode,
    VideoArtifact,
    VideoRequest,
)

_PROFILE = CapabilityProfile(
    modes=frozenset({{{modes_repr}}}),
    resolutions=frozenset({{"720P"}}),
    min_duration_s=1.0,
    max_duration_s=10.0,
    supports_negative_prompt=True,
    supports_seed=True,
)


class MyVideoPlugin:
    """A minimal, conformance-passing implementation. Fill in ``generate``."""

    capabilities = _PROFILE

    def __init__(self, *, config: dict, host: object) -> None:
        self._host = host
        self._config = config
        # Read your declared secret through the host handle, never from env:
        if {reads_secret}:
            self._api_key = host.host_secret("api_key")  # type: ignore[attr-defined]

    async def probe(self) -> ProbeResult:
        # A cheap, no-render credentials/liveness check. Return healthy=False with
        # a non-secret ``detail`` when the provider is unreachable.
        return ProbeResult(healthy=True, detail="stub probe")

    async def generate(self, request: VideoRequest) -> VideoArtifact:
        # TODO: call your model API here, e.g.:
        #   resp = await self._host.fetch("POST", self._config["base_url"], json=...)
        # then poll/await the clip URL and return it below.
        return VideoArtifact(
            clip_url="https://example.invalid/generated.mp4",
            duration_s=request.duration_s,
            model="{plugin_id}",
            mode=request.mode,
        )


def {factory_attr}(*, config: dict, host: object) -> MyVideoPlugin:
    """The entry-point factory the Kinora SDK calls to instantiate the plugin."""
    return MyVideoPlugin(config=config, host=host)
'''


__all__ = ["ScaffoldedPlugin", "create_plugin_template", "write_plugin_template"]
