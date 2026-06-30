"""A registry that builds any open-model adapter from a declarative config spec.

This is the composition seam: a single :func:`build_adapter` turns one
:class:`OpenAdapterSpec` (``kind`` + params + the two runtime gates) into a
router-ready backend, whether that's a hand-coded model adapter, a gateway
meta-adapter, or a config-only :class:`~.descriptor_adapter.DescriptorAdapter`.

``kind`` values:

* ``"descriptor"`` — load a :class:`ProviderDescriptor` from ``descriptor`` (a
  path / string / dict) and run it. **The zero-code path.**
* ``"replicate"`` / ``"fal"`` — the gateway meta-adapters by version / app-id.
* ``"stability"`` / ``"mochi"`` / ``"cogvideox"`` / ``"ltx"`` / ``"hunyuan"`` —
  the named open-model providers.

A whole *fleet* is built from a list of specs with :func:`build_fleet`, and the
bundled descriptors under ``descriptors/`` are discoverable via
:func:`bundled_descriptor_paths` / :func:`load_bundled`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import BaseOpenAdapter, PollConfig
from .descriptor import ProviderDescriptor, load_descriptor
from .descriptor_adapter import build_from_descriptor
from .fal import FalProvider
from .open_models import (
    CogVideoXProvider,
    HunyuanVideoProvider,
    LTXVideoProvider,
    MochiProvider,
    cogvideox_capabilities,
    hunyuan_capabilities,
    ltx_capabilities,
    mochi_capabilities,
)
from .replicate import InputMap, ReplicateProvider
from .stability_svd import StableVideoDiffusionProvider, svd_capabilities

__all__ = [
    "OpenAdapterSpec",
    "UnknownAdapterKind",
    "build_adapter",
    "build_fleet",
    "bundled_descriptor_paths",
    "load_bundled",
]

#: Where the bundled descriptor fixtures live.
_DESCRIPTOR_DIR = Path(__file__).parent / "descriptors"


class UnknownAdapterKind(ValueError):  # noqa: N818 - public name in the registry contract
    """Raised when an :class:`OpenAdapterSpec.kind` is not registered."""


@dataclass(frozen=True, slots=True)
class OpenAdapterSpec:
    """A declarative description of one adapter to build (no env reads).

    Attributes:
        kind: One of the registered kinds (see module docstring).
        name: Optional override for the backend identity.
        api_key: Provider credential (``None`` for unauthenticated self-hosted).
        allow_network: The OFF-by-default network gate for this adapter.
        live_video: The KINORA_LIVE_VIDEO spend gate for this adapter.
        params: Kind-specific params (``version`` / ``app_id`` / ``descriptor`` /
            ``input_map`` / ``base_url`` ...).
        poll: Optional polling bounds.
    """

    kind: str
    name: str | None = None
    api_key: str | None = None
    allow_network: bool = False
    live_video: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    poll: PollConfig | None = None


def build_adapter(
    spec: OpenAdapterSpec,
    *,
    usage_sink: Any | None = None,
    transport: object | None = None,
    settings: Any | None = None,
) -> BaseOpenAdapter:
    """Build one router-ready backend from ``spec``.

    ``transport`` is an injected httpx ``MockTransport`` (tests); production passes
    ``None`` so each adapter opens its own resilient client. ``settings`` lets a
    test supply a :class:`~app.core.config.Settings` so the inner client needs no
    env.
    """
    kind = spec.kind.lower()
    p = dict(spec.params)
    common = {
        "api_key": spec.api_key,
        "allow_network": spec.allow_network,
        "live_video": spec.live_video,
        "poll": spec.poll,
        "usage_sink": usage_sink,
        "transport": transport,
        "settings": settings,
    }

    if kind == "descriptor":
        descriptor = p.get("descriptor")
        if descriptor is None:
            raise UnknownAdapterKind("descriptor adapter requires params['descriptor']")
        return build_from_descriptor(
            descriptor,
            api_key=spec.api_key,
            allow_network=spec.allow_network,
            live_video=spec.live_video,
            poll=spec.poll,
            usage_sink=usage_sink,
            transport=transport,
            settings=settings,
        )

    if kind == "replicate":
        return ReplicateProvider.build(
            version=p["version"],
            capabilities=p["capabilities"],
            input_map=p.get("input_map") or InputMap(),
            model_label=spec.name or p.get("model_label", "replicate-model"),
            base_url=p.get("base_url", "https://api.replicate.com/v1"),
            **common,  # type: ignore[arg-type]
        )

    if kind == "fal":
        return FalProvider.build(
            app_id=p["app_id"],
            capabilities=p["capabilities"],
            input_map=p.get("input_map") or InputMap(),
            base_url=p.get("base_url", "https://queue.fal.run"),
            **common,  # type: ignore[arg-type]
        )

    if kind == "stability":
        return StableVideoDiffusionProvider.build(
            base_url=p.get("base_url", "https://api.stability.ai"),
            api_key=spec.api_key,
            allow_network=spec.allow_network,
            live_video=spec.live_video,
            poll=spec.poll,
            usage_sink=usage_sink,
            transport=transport,
            settings=settings,
        )

    named: dict[str, Any] = {
        "mochi": MochiProvider,
        "cogvideox": CogVideoXProvider,
        "ltx": LTXVideoProvider,
    }
    if kind in named:
        builder = named[kind]
        return builder.build(
            version=p["version"],
            api_key=spec.api_key,
            allow_network=spec.allow_network,
            live_video=spec.live_video,
            name=spec.name or kind,
            poll=spec.poll,
            usage_sink=usage_sink,
            transport=transport,
            settings=settings,
        )

    if kind == "hunyuan":
        return HunyuanVideoProvider.build(
            app_id=p.get("app_id", "fal-ai/hunyuan-video"),
            api_key=spec.api_key,
            allow_network=spec.allow_network,
            live_video=spec.live_video,
            name=spec.name or "hunyuan-video",
            poll=spec.poll,
            usage_sink=usage_sink,
            transport=transport,
            settings=settings,
        )

    raise UnknownAdapterKind(f"unknown open-adapter kind {spec.kind!r}")


def build_fleet(
    specs: Sequence[OpenAdapterSpec],
    *,
    usage_sink: Any | None = None,
    transport: object | None = None,
    settings: Any | None = None,
) -> list[BaseOpenAdapter]:
    """Build a list of backends from a list of specs (router-ready, in order)."""
    return [
        build_adapter(s, usage_sink=usage_sink, transport=transport, settings=settings)
        for s in specs
    ]


def bundled_descriptor_paths() -> list[Path]:
    """Every descriptor file shipped under ``descriptors/`` (sorted)."""
    if not _DESCRIPTOR_DIR.exists():
        return []
    return sorted(
        p for p in _DESCRIPTOR_DIR.iterdir() if p.suffix.lower() in (".yaml", ".yml", ".json")
    )


def load_bundled(stem: str) -> ProviderDescriptor:
    """Load a bundled descriptor by file stem (e.g. ``"comfyui_example"``)."""
    for path in bundled_descriptor_paths():
        if path.stem == stem:
            return load_descriptor(path)
    raise FileNotFoundError(f"no bundled descriptor with stem {stem!r}")


# Re-export the canonical capability builders so a spec author can reference them.
_CAPS = {
    "stability": svd_capabilities,
    "mochi": mochi_capabilities,
    "cogvideox": cogvideox_capabilities,
    "ltx": ltx_capabilities,
    "hunyuan": hunyuan_capabilities,
}
